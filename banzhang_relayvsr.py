"""👨‍🏫 班长 RelayVSR 大小模型接力视频超分（3 节点，自包含）。

算法层在 _relayvsr/，移植自 bbaudio-2025/Comfyui-Relay-VSR（Apache-2.0），
原始算法来自 kopperx/RelayVSR 与 ModelTC/LightX2V（均 Apache-2.0）。
本文件只做节点装配、中文界面、权重目录注册与模型缓存，不含算法。
"""

import os
import time
from pathlib import Path

import comfy.model_management as mm
import comfy.utils
import folder_paths
import torch
from comfy_api.latest import io, ui

from ._relayvsr.checkpoints import inspect_bundle
from ._relayvsr.runtime import RelayVSREngine
from ._relayvsr.streaming import (
    CODECS,
    open_image_source,
    open_video_source,
    reference_count,
    stream_upscale,
    upscale_chunk,
)
from ._relayvsr.tiling import axis_alphas, axis_tiles

_模型类型 = "RELAYVSR_MODEL"
_权重目录名 = "relayvsr"
_占位 = "（未找到 RelayVSR 权重）"
_类别 = "👨‍🏫班长/RelayVSR"

# 按本包所在位置算一个兜底根目录，不依赖 ComfyUI 启动时 models_dir 的最终状态。
_包内权重目录 = str(Path(__file__).resolve().parents[2] / "models" / _权重目录名)
folder_paths.add_model_folder_path(_权重目录名, os.path.join(folder_paths.models_dir, _权重目录名))

# 下拉列表是已保存工作流的稳定契约：safetensors 读取可能被瞬时的文件占用打断，
# 一次扫空不能让整个下拉塌成占位符、把整张图的校验打断。成功扫描会刷新这份缓存。
_权重包缓存 = {}


def _候选权重根目录():
    roots = list(folder_paths.get_folder_paths(_权重目录名))
    for candidate in (_包内权重目录, os.path.join(folder_paths.models_dir, _权重目录名)):
        if candidate not in roots:
            roots.append(candidate)
    return roots


def _是量化包(info):
    """查权重内部有没有 comfy_quant 标记，不靠目录名。"""
    from safetensors import safe_open

    with safe_open(str(info.wan_path), framework="pt", device="cpu") as handle:
        return any(key.endswith(".comfy_quant") for key in handle.keys())


def _扫描权重包():
    """按目录找配对的 wan / flash，倍数读 safetensors 内部标记，不靠文件名。"""
    found, checked, quantized = {}, [], {}
    for root_str in _候选权重根目录():
        root = Path(root_str)
        if not root.is_dir():
            checked.append(f"{root}（不存在）")
            continue
        checked.append(str(root))
        dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
        for candidate in [root, *dirs]:
            try:
                info = inspect_bundle(candidate)
            except ValueError:
                continue
            if info is None:
                continue
            label = candidate.name if candidate != root else root.name
            key = f"{label}（{info.scale}倍）"
            found[key] = info
            quantized[key] = _是量化包(info)
    if found:
        # 下拉第一项就是默认值，量化包画质有损失，必须排在原版权重后面。
        顺序 = dict(sorted(found.items(), key=lambda item: (quantized[item[0]], item[0])))
        _权重包缓存.clear()
        _权重包缓存.update(顺序)
        return 顺序
    if _权重包缓存:
        print(f"⚠️ [班长 RelayVSR] 本次没扫到权重（检查过：{'；'.join(checked)}），"
              f"沿用上次成功的列表，已保存的工作流不受影响。")
        return dict(_权重包缓存)
    print(f"⚠️ [班长 RelayVSR] 没找到权重包，检查过：{'；'.join(checked)}。"
          f"请确认目录下有配对的 wan/flash .safetensors。")
    return {}


# 启动时先扫一次给缓存播种；真扫不到时，上面那行日志会直接指出检查过哪些路径。
_扫描权重包()


def _取权重信息(权重包):
    info = _扫描权重包().get(权重包)
    if info is not None and not (info.wan_path.is_file() and info.flash_path.is_file()):
        info = None
    if info is None:
        raise ValueError(
            f"未找到 RelayVSR 权重。请把 wan / flash 两个 .safetensors 放进 "
            f"{os.path.join(folder_paths.models_dir, _权重目录名)} 下的子目录（例如 RelayVSR_X4）。"
        )
    return info


def _校验倍率(模型, 倍率):
    if 模型.scale == 2 and 倍率 != 2:
        raise ValueError("选中的是 2 倍权重，只能输出 2 倍；要 4 倍请在「权重包」里选带 4 倍的那一项。")


def _取视频对象(视频):
    # 包内 FlashVSR 等节点输出的是路径字符串，ComfyUI「加载视频」输出的是 VideoFromFile，两种都要能接。
    if hasattr(视频, "get_stream_source"):
        return 视频
    from comfy_api.latest._input_impl.video_types import VideoFromFile

    return VideoFromFile(str(视频))


def _图像转帧(图像):
    """IMAGE (B,H,W,3) 浮点 0~1 → 逐帧 uint8 (3,H,W)，与解码器交出的帧同格式。"""
    return [f.mul(255).round().clamp(0, 255).to(torch.uint8).cpu() for f in 图像.movedim(-1, 1)]


def _打开帧源(视频, 图像, 帧率, 编码器):
    """两个入口只能二选一：视频带音轨且帧率取文件本身，图像是内存里的帧序列。"""
    if 视频 is not None:
        return open_video_source(_取视频对象(视频), 编码器)
    if 图像 is not None:
        return open_image_source(_图像转帧(图像), 帧率)
    raise ValueError("RelayVSR：「视频」和「图像」至少接一个——整段视频接「视频」，帧序列接「图像」。")


def _解析注意力后端(启用):
    """加速开 → 把 ComfyUI 的 int8 注意力包成 SDPA 同签名；关或不可用 → None（原生 SDPA）。

    不可用只回退注意力内核，KV 仍按 int8 存（那只是存储编码，任何卡都能用），
    免得老卡一被回退就把显存翻倍。
    """
    if not 启用:
        return None
    try:
        import comfy_kitchen
        from comfy.ldm.modules.attention import (
            COMFY_KITCHEN_INT8_ATTENTION_IS_AVAILABLE,
            get_attention_function,
        )

        if not (COMFY_KITCHEN_INT8_ATTENTION_IS_AVAILABLE
                and comfy_kitchen.int8_attention_is_available(mm.get_torch_device())):
            raise RuntimeError("当前设备没有可用的 int8 注意力内核")
        fn = get_attention_function("comfy_kitchen_int8")
        if fn is None:
            raise RuntimeError("未注册 int8 注意力函数")
    except Exception as error:
        print(f"⚠️ [班长 RelayVSR] int8 注意力不可用（{error}），本次改用 pytorch 注意力"
              f"（KV 仍按 int8 存）。")
        return None

    def 调用(q, k, v, attn_mask=None, enable_gqa=False):
        return fn(q, k, v, heads=q.shape[1], mask=attn_mask,
                  skip_reshape=True, skip_output_reshape=True,
                  enable_gqa=bool(enable_gqa))

    print("✨ [班长 RelayVSR] 加速已开启：int8 注意力 + KV int8")
    return 调用


class _模型句柄:
    """加载器输出：只持权重路径，第一次超分时才把模型搬进显存。"""

    def __init__(self, info, dtype, attn_fn, kv_int8):
        self.info = info
        self.dtype = dtype
        self.scale = info.scale
        self.attn_fn = attn_fn
        self.kv_int8 = bool(kv_int8)
        self.engine = None

    def 取引擎(self):
        if self.engine is None:
            # 权重整份常驻显存；显存不够只会表现为变慢（WDDM 会挪用共享内存），
            # 所以不在这里猜，交给使用者在加载器里选合适的权重包。
            engine = RelayVSREngine(
                self.info, self.dtype, mm.get_torch_device(),
                self.attn_fn, self.kv_int8,
            )
            engine.load()
            self.engine = engine
        return self.engine

    def 释放(self):
        if self.engine is not None:
            self.engine.free()
            self.engine = None


class _可释放缓存(dict):
    """清空时先释放每个引擎的显存，再断引用。

    净化器按既有约定只对模块级字典调 clear()，所以释放职责放在容器里，不在净化器加分支。
    """

    def clear(self):
        for handle in self.values():
            handle.释放()
        super().clear()


# 同一个字典的两个入口：类变量供节点用，模块级名供净化器按既有约定取。
_模型缓存 = _可释放缓存()


def unload_all():
    """供外部显式卸载；净化器走的是 _模型缓存.clear()。"""
    _模型缓存.clear()


class BanZhangRelayVSRLoader:
    _模型缓存 = _模型缓存

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "权重包": (list(_扫描权重包()) or [_占位], {
                    "tooltip": "models/relayvsr 下配对好 wan / flash 权重的目录，括号里是它的原生倍率。\n"
                               "带 _W4A8 的是量化包，体积小但画质有损失；原版权重排在最前面。\n"
                               "整套权重会常驻显存：原版约 9.6G，W4A8 约 3.1G。"
                               "显存不足不会报错，只会被系统挪到共享内存而变慢，"
                               "8~12G 显卡请直接用 W4A8 包。",
                }),
                "精度": (["bf16（推荐）", "fp16"], {
                    "default": "bf16（推荐）",
                    "tooltip": "权重计算精度。Wan 权重原生 BF16、Flash 权重原生 FP32，都会统一转成这里选的精度。\n"
                               "Ampere 及以上（RTX 30/40/50、A100 等）：选 bf16，原生格式、数值最稳，官方默认。\n"
                               "Turing 及更早（GTX 10/16、RTX 20 系等 6G 老卡）：选 fp16，这些卡没有 bf16 硬件，"
                               "硬跑 bf16 会走软件模拟慢很多。\n"
                               "⚠ 两种都是 2 字节，换精度不省显存。实测 fp16 无 NaN、平均像素差 0.97/255。",
                }),
                "加速": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "开：注意力走 ComfyUI 官方 int8 内核，KV 缓存也压成 int8。\n"
                               "1080p 3×3 实测峰值 13.0G → 10.2G、快约 1.7 倍，画质差约 1.3/255，"
                               "8~16G 显卡推荐。\n"
                               "关：全部走原生 pytorch，最稳最准，但更占显存、更慢。\n"
                               "显卡不支持 int8 时，注意力自动回退到 pytorch，KV 仍按 int8 存（不会爆显存）。",
                }),
            },
        }

    RETURN_TYPES = (_模型类型,)
    RETURN_NAMES = ("模型",)
    FUNCTION = "加载"
    CATEGORY = _类别

    def 加载(self, 权重包, 精度, 加速):
        info = _取权重信息(权重包)
        dtype = "bf16" if 精度.startswith("bf16") else "fp16"
        attn_fn = _解析注意力后端(加速)
        key = (str(info.directory), dtype, bool(加速))
        cache = type(self)._模型缓存
        if key not in cache:
            # 换权重、精度或后端时先放掉旧模型，避免两套 10GB 权重同时占着显存。
            cache.clear()
            cache[key] = _模型句柄(info, dtype, attn_fn, 加速)
        return (cache[key],)


class BanZhangRelayVSR:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "模型": (_模型类型, {"tooltip": "接「班长 RelayVSR 模型加载」的输出"}),
                "图像": ("IMAGE", {"tooltip": "一整段连续帧，按帧顺序作为一个序列处理"}),
                "放大倍率": ([2, 4], {
                    "default": 2,
                    "tooltip": "2 倍权重只能用 2；4 倍权重可用 2 或 4",
                }),
                "时间块帧数": ("INT", {
                    "default": 16, "min": 2, "max": 64, "step": 1,
                    "tooltip": "每个流式窗口的帧数，越大越省窗口边界的重复计算，但更占显存",
                }),
                "空间分块": (list(_空间分块), {
                    "default": "3×3（推荐，8G 卡）",
                    "tooltip": "横纵各切几块，分块顺序跑，峰值显存只按单块算——降显存最有效的一招。\n"
                               "实测（int8 后端 + KVint8）：1080p 单帧 3×3 约 1.35G。\n"
                               "16G 以上显卡可调到「2×2」或「不分块」换速度。",
                }),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("图像",)
    FUNCTION = "超分"
    CATEGORY = _类别

    def 超分(self, 模型, 图像, 放大倍率, 时间块帧数, 空间分块):
        _校验倍率(模型, 放大倍率)
        engine = 模型.取引擎()
        frames = _图像转帧(图像)
        check = mm.throw_exception_if_processing_interrupted
        块数 = _空间分块[空间分块]
        if 块数 > 1:
            # 与流式节点共用同一套分块调度，不另写一份高度/融合逻辑。
            height, width = frames[0].shape[-2:]
            tiles_y = axis_tiles(height, 块数, _固定空间重叠, engine.geometry.wan_stride)
            tiles_x = axis_tiles(width, 块数, _固定空间重叠, engine.geometry.wan_stride)
            pixels = upscale_chunk(
                engine, frames, 放大倍率, 时间块帧数, _固定种子,
                tiles_y, tiles_x, axis_alphas(tiles_y, 放大倍率), axis_alphas(tiles_x, 放大倍率), check,
            )
        else:
            pixels = list(engine.restore(frames, _固定种子, 放大倍率, 时间块帧数, check,
                                         max_references=reference_count(len(frames), 时间块帧数)))
        progress = comfy.utils.ProgressBar(len(pixels))
        outputs = [torch.from_numpy(pixel).float().div(255.0) for pixel in pixels]
        progress.update(len(outputs))
        return (torch.stack(outputs),)


# 流式节点的固定项：这些参数调了收益极小，集中固定在这里，节点上不再暴露。
_固定输出宽度 = 0          # 0 = 源宽 × 放大倍率
_固定时间重叠 = 4          # stream_upscale 里会夹到「时间块帧数 - 1」
_固定空间重叠 = 64         # 64 是 wan_stride(16) 的整数倍，接缝融合够用
_固定CRF = 18.0
_编码预设 = "medium"       # PRESETS 里的「中等」
_固定种子 = 0              # 只影响参考帧初始噪声；固定值保证同一视频两次输出一致
_图像帧率 = 24.0           # 只在接「图像」口时生效

# 空间分块档位：列 = 行 = 档位。分块是唯一真正降显存的开关，合并成一个下拉。
_空间分块 = {
    "不分块（最快，吃显存）": 1,
    "2×2": 2,
    "3×3（推荐，8G 卡）": 3,
    "4×4": 4,
}


def _自动前缀():
    """按启动时间起名，两次跑不会互相覆盖。"""
    return "RelayVSR/upscaled_" + time.strftime("%Y%m%d_%H%M%S")


def _流式输入():
    return {
        "required": {
            "模型": (_模型类型, {"tooltip": "接「班长 RelayVSR 模型加载」的输出"}),
            "放大倍率": ([2, 4], {"default": 2, "tooltip": "2 倍权重只能用 2；4 倍权重可用 2 或 4"}),
            "时间块帧数": ("INT", {
                "default": 24, "min": 2, "max": 64, "step": 1,
                "tooltip": "每次处理的帧数，越大时间接缝越少、内存占用越高。\n"
                           "⚠ 不影响显存：块内是逐帧算的。爆显存请调「空间分块」或降低输入分辨率。",
            }),
            "空间分块": (list(_空间分块), {
                "default": "3×3（推荐，8G 卡）",
                "tooltip": "横纵各切几块，分块顺序跑，峰值显存只按单块算——降显存最有效的一招。\n"
                           "实测（int8 后端 + KVint8）：1080p 3×3 单帧约 1.35G。\n"
                           "16G 以上显卡可调到「2×2」或「不分块」换速度；8G 卡跑 1080p 请保持 3×3。\n"
                           "分块越小耗时越长，但不会爆显存。",
            }),
            "编码器": (list(CODECS), {
                "default": "h264",
                "tooltip": "h264 兼容性最好；h265 同质量更小；av1 最小但最慢，输出 WebM",
            }),
        },
        "optional": {
            "视频": ("VIDEO", {
                "tooltip": "接「加载视频」或本包 FlashVSR 的 VIDEO 输出。带音轨、帧率读文件自身，优先于「图像」。",
            }),
            "图像": ("IMAGE", {
                "tooltip": "接帧序列（VHS 类加载节点的图像输出）。没有音轨，输出为无声视频，固定 24fps。",
            }),
        },
    }


class BanZhangRelayVSRStreaming:
    @classmethod
    def INPUT_TYPES(cls):
        return _流式输入()

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("📹 视频对象", "📄 文件路径")
    FUNCTION = "流式超分"
    OUTPUT_NODE = True
    CATEGORY = _类别

    def 流式超分(self, 模型, 放大倍率, 时间块帧数, 空间分块, 编码器, 视频=None, 图像=None):
        _校验倍率(模型, 放大倍率)
        reader = _打开帧源(视频, 图像, _图像帧率, 编码器)
        块数 = _空间分块[空间分块]
        out_path, file_name, subfolder = stream_upscale(
            模型.取引擎(), reader, _固定输出宽度, 放大倍率, 时间块帧数, _固定种子,
            _固定时间重叠, 块数, 块数, _固定空间重叠, 编码器, _固定CRF,
            _编码预设, _自动前缀(), mm.throw_exception_if_processing_interrupted,
        )
        preview = ui.PreviewVideo([ui.SavedResult(file_name, subfolder, io.FolderType.output)])
        # 视频口直接给路径：与「视频合并」的「📹 视频对象」同口径，下游 Load Video / 合并 / 保存都能接。
        return {"ui": preview.as_dict(), "result": (out_path, out_path)}


NODE_CLASS_MAPPINGS = {
    "BanZhangRelayVSRLoader": BanZhangRelayVSRLoader,
    "BanZhangRelayVSR": BanZhangRelayVSR,
    "BanZhangRelayVSRStreaming": BanZhangRelayVSRStreaming,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BanZhangRelayVSRLoader": "👨‍🏫 班长 RelayVSR 模型加载",
    "BanZhangRelayVSR": "👨‍🏫 班长 RelayVSR 图像超分",
    "BanZhangRelayVSRStreaming": "👨‍🏫 班长 RelayVSR 视频流式超分",
}
