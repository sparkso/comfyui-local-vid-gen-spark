"""ComfyUI API-format prompt builders.

Each builder returns a dict in ComfyUI's /prompt API format ({node_id: {class_type, inputs}}).
The graphs mirror the UI workflows saved on tigerclaw (workspace/comfyui/*.json) and White-PC
(ComfyUI user workflows), which are ComfyUI's bundled templates, with subgraphs flattened by hand:

  video pipelines
  * ltx2_t2v      "Text to Video (LTX 2 Distilled)"  two-stage: half-res sample -> 2x latent upsample -> refine
  * ltx2_i2v      "Image to Video (LTX 2 Distilled)" same, seeded with a preprocessed still
  * wan22_t2v     "Wan 2.2 14B T2V" 4-step lightx2v LoRA branch (high-noise -> low-noise)
  * wan22_t2v_hq  the bypassed 20-step / cfg 3.5 branch of the same template
  * wan22_5b_t2v  "Wan 2.2 5B TI2V" 16-step uni_pc, no start image
  * wan22_5b_i2v  same with a start image (Wan22ImageToVideoLatent)

  image pipelines (single-frame renders with the same video models; no dedicated image model is installed)
  * wan22_t2i     Wan 2.2 14B, length=1, 4-step LoRA
  * wan22_t2i_hq  Wan 2.2 14B, length=1, 20-step cfg 3.5
  * wan22_i2i     Wan 2.2 14B low-noise expert + LoRA, VAE-encoded input, partial denoise (strength)

Constraints (from the template notes):
  LTX-2: width/height divisible by 32, frames = 8n+1 (max 241).
  Wan 14B: width/height divisible by 16, frames = 4n+1 (81-161 recommended).
  Wan 5B: width/height divisible by 32, frames = 4n+1.
"""

LTX_STAGE1_SIGMAS = "1., 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"
LTX_STAGE2_SIGMAS = "0.909375, 0.725, 0.421875, 0.0"

WAN_NEGATIVE_DEFAULT = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，"
    "低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，"
    "毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
)

MODELS = {
    "ltx_ckpt": "ltx-2-19b-distilled.safetensors",
    "ltx_upscaler": "ltx-2-spatial-upscaler-x2-1.0.safetensors",
    "ltx_text_encoder": "gemma_3_12B_it_fp4_mixed.safetensors",
    "wan_high": "wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors",
    "wan_low": "wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors",
    "wan_lora_high": "wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors",
    "wan_lora_low": "wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors",
    "wan_clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
    "wan_vae": "wan_2.1_vae.safetensors",
    "wan5b_unet": "wan2.2_ti2v_5B_fp16.safetensors",
    "wan5b_vae": "wan2.2_vae.safetensors",
    "ltx25_unet": "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors",
    "ltx25_video_vae": "ltx-2.5-video-vae-bf16.safetensors",
    "ltx25_audio_vae": "ltx-2.5-audio-vae-bf16.safetensors",
    "ltx25_text_encoder": "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
    "ltx25_upscaler": "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
}

LTX25_STAGE1_SIGMAS = "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"
LTX25_STAGE2_SIGMAS = "0.85, 0.7250, 0.4219, 0.0"
LTX25_NEGATIVE = "pc game, console game, video game, cartoon, childish, ugly"

TASKS = {
    "t2i": "Text → Image",
    "t2v": "Text → Video",
    "i2i": "Image → Image",
    "i2v": "Image → Video",
}

_LTX_SIZES = [
    ["Portrait 1088×1920", 1088, 1920],
    ["Landscape 1920×1088", 1920, 1088],
    ["Landscape 1280×720", 1280, 720],
    ["Portrait 720×1280", 720, 1280],
    ["Square 1024×1024", 1024, 1024],
]
_WAN14_VIDEO_SIZES = [
    ["Portrait 480×832", 480, 832],
    ["Landscape 832×480", 832, 480],
    ["Square 640×640", 640, 640],
    ["Portrait 720×1280", 720, 1280],
    ["Landscape 1280×720", 1280, 720],
]
_WAN5_VIDEO_SIZES = [
    ["Portrait 704×1280", 704, 1280],
    ["Landscape 1280×704", 1280, 704],
    ["Square 960×960", 960, 960],
    ["Portrait 480×832", 480, 832],
]
_IMAGE_SIZES = [
    ["Square 1024×1024", 1024, 1024],
    ["Portrait 832×1216", 832, 1216],
    ["Landscape 1216×832", 1216, 832],
    ["Portrait 720×1280", 720, 1280],
    ["Landscape 1280×720", 1280, 720],
    ["Portrait 1088×1920", 1088, 1920],
]
_LTX_FRAMES = [49, 73, 97, 121, 145, 169, 193, 217, 241]
_WAN_FRAMES = [41, 61, 81, 101, 121, 141, 161]

# Presets shown in the UI. width/height are the FINAL output size.
PRESETS = {
    # ---- text -> video
    "ltx25_t2v": {"task": "t2v", "label": "LTX-2.5 22B distilled (audio)", "sizes": _LTX_SIZES,
                  "frames": _LTX_FRAMES, "default_frames": 121, "fps": 24, "needs_image": False, "output": "video"},
    "ltx2_t2v": {"task": "t2v", "label": "LTX-2 19B distilled (audio, fast)", "sizes": _LTX_SIZES,
                 "frames": _LTX_FRAMES, "default_frames": 121, "fps": 24, "needs_image": False, "output": "video"},
    "wan22_t2v": {"task": "t2v", "label": "Wan 2.2 14B · 4-step LoRA", "sizes": _WAN14_VIDEO_SIZES,
                  "frames": _WAN_FRAMES, "default_frames": 81, "fps": 16, "needs_image": False, "output": "video"},
    "wan22_t2v_hq": {"task": "t2v", "label": "Wan 2.2 14B · 20-step HQ (slow)", "sizes": _WAN14_VIDEO_SIZES,
                     "frames": _WAN_FRAMES, "default_frames": 81, "fps": 16, "needs_image": False, "output": "video"},
    "wan22_5b_t2v": {"task": "t2v", "label": "Wan 2.2 5B TI2V · 16-step", "sizes": _WAN5_VIDEO_SIZES,
                     "frames": _WAN_FRAMES, "default_frames": 121, "fps": 24, "needs_image": False, "output": "video"},
    # ---- image -> video
    "ltx25_i2v": {"task": "i2v", "label": "LTX-2.5 22B distilled (audio)", "sizes": _LTX_SIZES,
                  "frames": _LTX_FRAMES, "default_frames": 121, "fps": 24, "needs_image": True, "output": "video"},
    "ltx2_i2v": {"task": "i2v", "label": "LTX-2 19B distilled (audio, fast)", "sizes": _LTX_SIZES,
                 "frames": _LTX_FRAMES, "default_frames": 121, "fps": 24, "needs_image": True, "output": "video"},
    "wan22_5b_i2v": {"task": "i2v", "label": "Wan 2.2 5B TI2V · 16-step", "sizes": _WAN5_VIDEO_SIZES,
                     "frames": _WAN_FRAMES, "default_frames": 121, "fps": 24, "needs_image": True, "output": "video"},
    # ---- text -> image
    "wan22_t2i": {"task": "t2i", "label": "Wan 2.2 14B · 4-step LoRA", "sizes": _IMAGE_SIZES,
                  "frames": [1], "default_frames": 1, "fps": 1, "needs_image": False, "output": "image"},
    "wan22_t2i_hq": {"task": "t2i", "label": "Wan 2.2 14B · 20-step HQ", "sizes": _IMAGE_SIZES,
                     "frames": [1], "default_frames": 1, "fps": 1, "needs_image": False, "output": "image"},
    # ---- image -> image
    "wan22_i2i": {"task": "i2i", "label": "Wan 2.2 14B low-noise · img2img (strength)", "sizes": _IMAGE_SIZES,
                  "frames": [1], "default_frames": 1, "fps": 1, "needs_image": True, "output": "image",
                  "has_strength": True},
    # ---- post-process (not shown in the task picker)
    "faceswap": {"task": "fix", "label": "ReActor face swap (inswapper_128 + GFPGAN)", "sizes": [],
                 "frames": [1], "default_frames": 1, "fps": 0, "needs_image": True, "output": "video", "hidden": True},
}


def build_faceswap(video_file, face_image, restore=True, filename_prefix="video/localvidgen/job",
                   visibility=1.0, codeformer_weight=0.5, **_):
    """Swap the reference face into every frame of an uploaded clip (ComfyUI-ReActor), keep the original audio."""
    g = {
        "vid": {"class_type": "LoadVideo", "inputs": {"file": video_file}},
        "comp": {"class_type": "GetVideoComponents", "inputs": {"video": ["vid", 0]}},
        "face": {"class_type": "LoadImage", "inputs": {"image": face_image}},
        "swap": {
            "class_type": "ReActorFaceSwap",
            "inputs": {
                "enabled": True,
                "input_image": ["comp", 0],
                "source_image": ["face", 0],
                "swap_model": "inswapper_128.onnx",
                "facedetection": "retinaface_resnet50",
                "face_restore_model": "GFPGANv1.4.pth" if restore else "none",
                "face_restore_visibility": float(visibility),
                "codeformer_weight": float(codeformer_weight),
                "detect_gender_input": "no",
                "detect_gender_source": "no",
                "input_faces_index": "0",
                "source_faces_index": "0",
                "console_log_level": 1,
            },
        },
        "video": {"class_type": "CreateVideo", "inputs": {"images": ["swap", 0], "audio": ["comp", 1], "fps": ["comp", 2]}},
        "save": {"class_type": "SaveVideo",
                 "inputs": {"video": ["video", 0], "filename_prefix": filename_prefix, "format": "auto", "codec": "auto"}},
    }
    return g


def _snap(value, step, minimum):
    value = max(minimum, int(value))
    return value - (value % step)


def _frames(n, step, lo, hi):
    n = int(n)
    if n % step != 1:
        n = (n // step) * step + 1
    return max(lo, min(hi, n))


def _save(g, output, filename_prefix, fps=None):
    if output == "image":
        g["save"] = {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": filename_prefix}}
    else:
        g["video"] = {"class_type": "CreateVideo", "inputs": {"images": ["decode", 0], "fps": float(fps)}}
        g["save"] = {"class_type": "SaveVideo",
                     "inputs": {"video": ["video", 0], "filename_prefix": filename_prefix, "format": "auto", "codec": "auto"}}


# ----------------------------------------------------------------------------- LTX-2
def _ltx_common(g, prompt, ckpt, text_encoder, fps):
    g["ckpt"] = {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}}
    g["audio_vae"] = {"class_type": "LTXVAudioVAELoader", "inputs": {"ckpt_name": ckpt}}
    g["te"] = {"class_type": "LTXAVTextEncoderLoader",
               "inputs": {"text_encoder": text_encoder, "ckpt_name": ckpt, "device": "default"}}
    g["pos"] = {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["te", 0]}}
    g["neg"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}}
    g["cond"] = {"class_type": "LTXVConditioning",
                 "inputs": {"positive": ["pos", 0], "negative": ["neg", 0], "frame_rate": float(fps)}}
    g["upscaler"] = {"class_type": "LatentUpscaleModelLoader", "inputs": {"model_name": MODELS["ltx_upscaler"]}}


def _ltx_sampler(g, tag, model, positive, negative, sampler_name, sigmas, seed, latent):
    g[f"guider{tag}"] = {"class_type": "CFGGuider",
                         "inputs": {"model": model, "positive": positive, "negative": negative, "cfg": 1.0}}
    g[f"sampler{tag}"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampler_name}}
    g[f"sigmas{tag}"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": sigmas}}
    g[f"noise{tag}"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}}
    g[f"samp{tag}"] = {"class_type": "SamplerCustomAdvanced",
                       "inputs": {"noise": [f"noise{tag}", 0], "guider": [f"guider{tag}", 0], "sampler": [f"sampler{tag}", 0],
                                  "sigmas": [f"sigmas{tag}", 0], "latent_image": latent}}


def build_ltx2_t2v(prompt, width, height, frames, seed, fps=24, filename_prefix="video/localvidgen/job", **_):
    width, height = _snap(width, 32, 256), _snap(height, 32, 256)
    frames = _frames(frames, 8, 9, 241)
    g = {}
    _ltx_common(g, prompt, MODELS["ltx_ckpt"], MODELS["ltx_text_encoder"], fps)
    # Stage 1 at half resolution (template: EmptyImage -> ImageScaleBy 0.5 -> GetImageSize).
    g["latent1"] = {"class_type": "EmptyLTXVLatentVideo",
                    "inputs": {"width": width // 2, "height": height // 2, "length": frames, "batch_size": 1}}
    g["audio_latent"] = {"class_type": "LTXVEmptyLatentAudio",
                         "inputs": {"frames_number": frames, "frame_rate": int(fps), "batch_size": 1, "audio_vae": ["audio_vae", 0]}}
    g["concat1"] = {"class_type": "LTXVConcatAVLatent",
                    "inputs": {"video_latent": ["latent1", 0], "audio_latent": ["audio_latent", 0]}}
    _ltx_sampler(g, "1", ["ckpt", 0], ["cond", 0], ["cond", 1], "euler_ancestral", LTX_STAGE1_SIGMAS, seed, ["concat1", 0])
    g["sep1"] = {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["samp1", 1]}}
    # Stage 2: 2x latent upsample and refine.
    g["up"] = {"class_type": "LTXVLatentUpsampler",
               "inputs": {"samples": ["sep1", 0], "upscale_model": ["upscaler", 0], "vae": ["ckpt", 2]}}
    g["concat2"] = {"class_type": "LTXVConcatAVLatent", "inputs": {"video_latent": ["up", 0], "audio_latent": ["sep1", 1]}}
    _ltx_sampler(g, "2", ["ckpt", 0], ["cond", 0], ["cond", 1], "euler_ancestral", LTX_STAGE2_SIGMAS, int(seed) + 1, ["concat2", 0])
    g["sep2"] = {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["samp2", 1]}}
    g["decode"] = {"class_type": "VAEDecodeTiled",
                   "inputs": {"samples": ["sep2", 0], "vae": ["ckpt", 2], "tile_size": 512, "overlap": 64,
                              "temporal_size": 4096, "temporal_overlap": 8}}
    g["adecode"] = {"class_type": "LTXVAudioVAEDecode", "inputs": {"samples": ["sep2", 1], "audio_vae": ["audio_vae", 0]}}
    g["video"] = {"class_type": "CreateVideo", "inputs": {"images": ["decode", 0], "audio": ["adecode", 0], "fps": float(fps)}}
    g["save"] = {"class_type": "SaveVideo",
                 "inputs": {"video": ["video", 0], "filename_prefix": filename_prefix, "format": "auto", "codec": "auto"}}
    return g


def build_ltx2_i2v(prompt, image, width, height, frames, seed, fps=24, filename_prefix="video/localvidgen/job", **_):
    width, height = _snap(width, 32, 256), _snap(height, 32, 256)
    frames = _frames(frames, 8, 9, 241)
    g = {}
    _ltx_common(g, prompt, MODELS["ltx_ckpt"], MODELS["ltx_text_encoder"], fps)
    g["img"] = {"class_type": "LoadImage", "inputs": {"image": image}}
    # Template uses ResizeImageMaskNode(scale dimensions, W, H, center, lanczos); ImageScale is equivalent.
    g["img_fit"] = {"class_type": "ImageScale",
                    "inputs": {"image": ["img", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}}
    g["img_edge"] = {"class_type": "ResizeImagesByLongerEdge", "inputs": {"images": ["img_fit", 0], "longer_edge": 1536}}
    g["img_pre"] = {"class_type": "LTXVPreprocess", "inputs": {"image": ["img_edge", 0], "img_compression": 33}}
    g["latent1"] = {"class_type": "EmptyLTXVLatentVideo",
                    "inputs": {"width": width // 2, "height": height // 2, "length": frames, "batch_size": 1}}
    g["i2v1"] = {"class_type": "LTXVImgToVideoInplace",
                 "inputs": {"vae": ["ckpt", 2], "image": ["img_pre", 0], "latent": ["latent1", 0], "strength": 1.0, "bypass": False}}
    g["audio_latent"] = {"class_type": "LTXVEmptyLatentAudio",
                         "inputs": {"frames_number": frames, "frame_rate": int(fps), "batch_size": 1, "audio_vae": ["audio_vae", 0]}}
    g["concat1"] = {"class_type": "LTXVConcatAVLatent",
                    "inputs": {"video_latent": ["i2v1", 0], "audio_latent": ["audio_latent", 0]}}
    _ltx_sampler(g, "1", ["ckpt", 0], ["cond", 0], ["cond", 1], "euler", LTX_STAGE1_SIGMAS, seed, ["concat1", 0])
    # i2v template takes SamplerCustomAdvanced.output (index 0), not denoised_output.
    g["sep1"] = {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["samp1", 0]}}
    g["crop"] = {"class_type": "LTXVCropGuides", "inputs": {"positive": ["cond", 0], "negative": ["cond", 1], "latent": ["sep1", 0]}}
    g["up"] = {"class_type": "LTXVLatentUpsampler",
               "inputs": {"samples": ["crop", 2], "upscale_model": ["upscaler", 0], "vae": ["ckpt", 2]}}
    g["i2v2"] = {"class_type": "LTXVImgToVideoInplace",
                 "inputs": {"vae": ["ckpt", 2], "image": ["img_pre", 0], "latent": ["up", 0], "strength": 1.0, "bypass": False}}
    g["concat2"] = {"class_type": "LTXVConcatAVLatent", "inputs": {"video_latent": ["i2v2", 0], "audio_latent": ["sep1", 1]}}
    _ltx_sampler(g, "2", ["ckpt", 0], ["crop", 0], ["crop", 1], "gradient_estimation", LTX_STAGE2_SIGMAS, int(seed) + 1, ["concat2", 0])
    g["sep2"] = {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["samp2", 1]}}
    g["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["sep2", 0], "vae": ["ckpt", 2]}}
    g["adecode"] = {"class_type": "LTXVAudioVAEDecode", "inputs": {"samples": ["sep2", 1], "audio_vae": ["audio_vae", 0]}}
    g["video"] = {"class_type": "CreateVideo", "inputs": {"images": ["decode", 0], "audio": ["adecode", 0], "fps": float(fps)}}
    g["save"] = {"class_type": "SaveVideo",
                 "inputs": {"video": ["video", 0], "filename_prefix": filename_prefix, "format": "auto", "codec": "auto"}}
    return g


# ----------------------------------------------------------------------------- LTX-2.5
def _ltx25_common(g, prompt, fps, negative=None):
    """Loaders + conditioning exactly as the bundled 'video_ltx2_5_*' templates (prompt enhancer switched off)."""
    g["unet"] = {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["ltx25_unet"], "weight_dtype": "default"}}
    g["vae"] = {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["ltx25_video_vae"]}}
    g["audio_vae"] = {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["ltx25_audio_vae"]}}
    g["clip"] = {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": MODELS["ltx25_text_encoder"], "type": "ltxv", "device": "default"}}
    g["pos"] = {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}}
    g["neg"] = {"class_type": "CLIPTextEncode", "inputs": {"text": negative or LTX25_NEGATIVE, "clip": ["clip", 0]}}
    g["cond"] = {"class_type": "LTXVConditioning",
                 "inputs": {"positive": ["pos", 0], "negative": ["neg", 0], "frame_rate": float(fps)}}
    g["upscaler"] = {"class_type": "LatentUpscaleModelLoader", "inputs": {"model_name": MODELS["ltx25_upscaler"]}}


def _ltx25_sampler(g, tag, sigmas, seed, latent):
    g[f"guider{tag}"] = {"class_type": "LTXVDualCFGGuider",
                         "inputs": {"model": ["unet", 0], "positive": ["cond", 0], "negative": ["cond", 1],
                                    "video_cfg": 1.0, "audio_cfg": 1.0}}
    g[f"sampler{tag}"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler_ancestral"}}
    g[f"sigmas{tag}"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": sigmas}}
    g[f"noise{tag}"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}}
    g[f"samp{tag}"] = {"class_type": "SamplerCustomAdvanced",
                       "inputs": {"noise": [f"noise{tag}", 0], "guider": [f"guider{tag}", 0], "sampler": [f"sampler{tag}", 0],
                                  "sigmas": [f"sigmas{tag}", 0], "latent_image": latent}}
    # 2.5 templates take SamplerCustomAdvanced.output (slot 0) at both stages.
    g[f"sep{tag}"] = {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": [f"samp{tag}", 0]}}


def _ltx25_finish(g, fps, filename_prefix):
    g["decode"] = {"class_type": "VAEDecodeTiled",
                   "inputs": {"samples": ["sep2", 0], "vae": ["vae", 0], "tile_size": 512, "overlap": 64,
                              "temporal_size": 64, "temporal_overlap": 16}}
    g["adecode"] = {"class_type": "LTXVAudioVAEDecode", "inputs": {"samples": ["sep2", 1], "audio_vae": ["audio_vae", 0]}}
    g["video"] = {"class_type": "CreateVideo", "inputs": {"images": ["decode", 0], "audio": ["adecode", 0], "fps": float(fps)}}
    g["save"] = {"class_type": "SaveVideo",
                 "inputs": {"video": ["video", 0], "filename_prefix": filename_prefix, "format": "auto", "codec": "auto"}}


def build_ltx25_t2v(prompt, width, height, frames, seed, fps=24, negative=None,
                    filename_prefix="video/localvidgen/job", **_):
    width, height = _snap(width, 32, 256), _snap(height, 32, 256)
    frames = _frames(frames, 8, 9, 241)
    g = {}
    _ltx25_common(g, prompt, fps, negative)
    g["latent1"] = {"class_type": "EmptyLTXVLatentVideo",
                    "inputs": {"width": width // 2, "height": height // 2, "length": frames, "batch_size": 1}}
    g["audio_latent"] = {"class_type": "LTXVEmptyLatentAudio",
                         "inputs": {"frames_number": frames, "frame_rate": int(fps), "batch_size": 1, "audio_vae": ["audio_vae", 0]}}
    g["concat1"] = {"class_type": "LTXVConcatAVLatent",
                    "inputs": {"video_latent": ["latent1", 0], "audio_latent": ["audio_latent", 0]}}
    _ltx25_sampler(g, "1", LTX25_STAGE1_SIGMAS, seed, ["concat1", 0])
    g["up"] = {"class_type": "LTXVLatentUpsampler",
               "inputs": {"samples": ["sep1", 0], "upscale_model": ["upscaler", 0], "vae": ["vae", 0]}}
    g["concat2"] = {"class_type": "LTXVConcatAVLatent", "inputs": {"video_latent": ["up", 0], "audio_latent": ["sep1", 1]}}
    _ltx25_sampler(g, "2", LTX25_STAGE2_SIGMAS, 42, ["concat2", 0])
    _ltx25_finish(g, fps, filename_prefix)
    return g


def build_ltx25_i2v(prompt, image, width, height, frames, seed, fps=24, negative=None,
                    filename_prefix="video/localvidgen/job", **_):
    width, height = _snap(width, 32, 256), _snap(height, 32, 256)
    frames = _frames(frames, 8, 9, 241)
    g = {}
    _ltx25_common(g, prompt, fps, negative)
    g["img"] = {"class_type": "LoadImage", "inputs": {"image": image}}
    g["img_fit"] = {"class_type": "ImageScale",
                    "inputs": {"image": ["img", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}}
    g["img_edge"] = {"class_type": "ResizeImagesByLongerEdge", "inputs": {"images": ["img_fit", 0], "longer_edge": 1536}}
    g["img_pre"] = {"class_type": "LTXVPreprocess", "inputs": {"image": ["img_edge", 0], "img_compression": 18}}
    g["latent1"] = {"class_type": "EmptyLTXVLatentVideo",
                    "inputs": {"width": width // 2, "height": height // 2, "length": frames, "batch_size": 1}}
    g["i2v1"] = {"class_type": "LTXVImgToVideoInplace",
                 "inputs": {"vae": ["vae", 0], "image": ["img_pre", 0], "latent": ["latent1", 0], "strength": 0.7, "bypass": False}}
    g["audio_latent"] = {"class_type": "LTXVEmptyLatentAudio",
                         "inputs": {"frames_number": frames, "frame_rate": int(fps), "batch_size": 1, "audio_vae": ["audio_vae", 0]}}
    g["concat1"] = {"class_type": "LTXVConcatAVLatent",
                    "inputs": {"video_latent": ["i2v1", 0], "audio_latent": ["audio_latent", 0]}}
    _ltx25_sampler(g, "1", LTX25_STAGE1_SIGMAS, seed, ["concat1", 0])
    g["up"] = {"class_type": "LTXVLatentUpsampler",
               "inputs": {"samples": ["sep1", 0], "upscale_model": ["upscaler", 0], "vae": ["vae", 0]}}
    g["i2v2"] = {"class_type": "LTXVImgToVideoInplace",
                 "inputs": {"vae": ["vae", 0], "image": ["img_pre", 0], "latent": ["up", 0], "strength": 1.0, "bypass": False}}
    g["concat2"] = {"class_type": "LTXVConcatAVLatent", "inputs": {"video_latent": ["i2v2", 0], "audio_latent": ["sep1", 1]}}
    _ltx25_sampler(g, "2", LTX25_STAGE2_SIGMAS, 42, ["concat2", 0])
    _ltx25_finish(g, fps, filename_prefix)
    return g


# ----------------------------------------------------------------------------- Wan 2.2 14B
def _wan14_common(g, prompt, negative):
    g["clip"] = {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS["wan_clip"], "type": "wan", "device": "default"}}
    g["vae"] = {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["wan_vae"]}}
    g["pos"] = {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}}
    g["neg"] = {"class_type": "CLIPTextEncode", "inputs": {"text": negative or WAN_NEGATIVE_DEFAULT, "clip": ["clip", 0]}}


def build_wan22(prompt, width, height, frames, seed, fps=16, negative=None, hq=False, output="video",
                filename_prefix="video/localvidgen/job", **_):
    """Two-expert Wan 2.2 14B: high-noise sampler then low-noise sampler. frames=1 gives a still image."""
    width, height = _snap(width, 16, 256), _snap(height, 16, 256)
    frames = 1 if output == "image" else _frames(frames, 4, 5, 241)
    g = {}
    _wan14_common(g, prompt, negative)
    g["unet_high"] = {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["wan_high"], "weight_dtype": "default"}}
    g["unet_low"] = {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["wan_low"], "weight_dtype": "default"}}
    g["latent"] = {"class_type": "EmptyHunyuanLatentVideo",
                   "inputs": {"width": width, "height": height, "length": frames, "batch_size": 1}}
    if hq:
        shift, steps, cfg, split = 8.0, 20, 3.5, 10
        high_model, low_model = ["unet_high", 0], ["unet_low", 0]
    else:
        shift, steps, cfg, split = 5.0, 4, 1.0, 2
        g["lora_high"] = {"class_type": "LoraLoaderModelOnly",
                          "inputs": {"model": ["unet_high", 0], "lora_name": MODELS["wan_lora_high"], "strength_model": 1.0}}
        g["lora_low"] = {"class_type": "LoraLoaderModelOnly",
                         "inputs": {"model": ["unet_low", 0], "lora_name": MODELS["wan_lora_low"], "strength_model": 1.0}}
        high_model, low_model = ["lora_high", 0], ["lora_low", 0]
    g["ms_high"] = {"class_type": "ModelSamplingSD3", "inputs": {"model": high_model, "shift": shift}}
    g["ms_low"] = {"class_type": "ModelSamplingSD3", "inputs": {"model": low_model, "shift": shift}}
    g["ks_high"] = {"class_type": "KSamplerAdvanced",
                    "inputs": {"model": ["ms_high", 0], "add_noise": "enable", "noise_seed": int(seed), "steps": steps, "cfg": cfg,
                               "sampler_name": "euler", "scheduler": "simple", "positive": ["pos", 0], "negative": ["neg", 0],
                               "latent_image": ["latent", 0], "start_at_step": 0, "end_at_step": split,
                               "return_with_leftover_noise": "enable"}}
    g["ks_low"] = {"class_type": "KSamplerAdvanced",
                   "inputs": {"model": ["ms_low", 0], "add_noise": "disable", "noise_seed": 0, "steps": steps, "cfg": cfg,
                              "sampler_name": "euler", "scheduler": "simple", "positive": ["pos", 0], "negative": ["neg", 0],
                              "latent_image": ["ks_high", 0], "start_at_step": split, "end_at_step": 10000,
                              "return_with_leftover_noise": "disable"}}
    g["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["ks_low", 0], "vae": ["vae", 0]}}
    _save(g, output, filename_prefix, fps)
    return g


def build_wan22_i2i(prompt, image, width, height, seed, strength=0.6, negative=None,
                    filename_prefix="localvidgen/job", **_):
    """img2img: VAE-encode the still, then partially denoise with the low-noise expert + 4-step LoRA."""
    width, height = _snap(width, 16, 256), _snap(height, 16, 256)
    strength = min(1.0, max(0.05, float(strength)))
    g = {}
    _wan14_common(g, prompt, negative)
    g["img"] = {"class_type": "LoadImage", "inputs": {"image": image}}
    g["img_fit"] = {"class_type": "ImageScale",
                    "inputs": {"image": ["img", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}}
    g["latent"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["img_fit", 0], "vae": ["vae", 0]}}
    g["unet_low"] = {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["wan_low"], "weight_dtype": "default"}}
    g["lora_low"] = {"class_type": "LoraLoaderModelOnly",
                     "inputs": {"model": ["unet_low", 0], "lora_name": MODELS["wan_lora_low"], "strength_model": 1.0}}
    g["ms_low"] = {"class_type": "ModelSamplingSD3", "inputs": {"model": ["lora_low", 0], "shift": 5.0}}
    # 8 steps so that denoise=strength still leaves several real steps for the 4-step LoRA.
    g["ks"] = {"class_type": "KSampler",
               "inputs": {"model": ["ms_low", 0], "seed": int(seed), "steps": 8, "cfg": 1.0, "sampler_name": "euler",
                          "scheduler": "simple", "positive": ["pos", 0], "negative": ["neg", 0],
                          "latent_image": ["latent", 0], "denoise": strength}}
    g["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}}
    _save(g, "image", filename_prefix)
    return g


# ----------------------------------------------------------------------------- Wan 2.2 5B TI2V
def build_wan22_5b(prompt, width, height, frames, seed, fps=24, negative=None, image=None,
                   filename_prefix="video/localvidgen/job", **_):
    width, height = _snap(width, 32, 256), _snap(height, 32, 256)
    frames = _frames(frames, 4, 5, 241)
    g = {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": MODELS["wan5b_unet"], "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": MODELS["wan_clip"], "type": "wan", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": MODELS["wan5b_vae"]}},
        "ms": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["unet", 0], "shift": 8.0}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": negative or WAN_NEGATIVE_DEFAULT, "clip": ["clip", 0]}},
    }
    lat = {"vae": ["vae", 0], "width": width, "height": height, "length": frames, "batch_size": 1}
    if image:
        g["img"] = {"class_type": "LoadImage", "inputs": {"image": image}}
        g["img_fit"] = {"class_type": "ImageScale",
                        "inputs": {"image": ["img", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}}
        lat["start_image"] = ["img_fit", 0]
    g["latent"] = {"class_type": "Wan22ImageToVideoLatent", "inputs": lat}
    g["ks"] = {"class_type": "KSampler",
               "inputs": {"model": ["ms", 0], "seed": int(seed), "steps": 16, "cfg": 5.0, "sampler_name": "uni_pc",
                          "scheduler": "simple", "positive": ["pos", 0], "negative": ["neg", 0],
                          "latent_image": ["latent", 0], "denoise": 1.0}}
    g["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}}
    _save(g, "video", filename_prefix, fps)
    return g


# ----------------------------------------------------------------------------- dispatch
def build(mode, params, filename_prefix):
    """Dispatch on pipeline id. `params` is the job dict from the UI."""
    preset = PRESETS[mode]
    kw = dict(
        prompt=params["prompt"],
        width=params["width"],
        height=params["height"],
        frames=params.get("frames") or preset["default_frames"],
        seed=params["seed"],
        fps=params.get("fps") or preset["fps"],
        negative=params.get("negative"),
        image=params.get("image"),
        strength=params.get("strength", 0.6),
        filename_prefix=filename_prefix,
    )
    if mode == "ltx2_t2v":
        return build_ltx2_t2v(**kw)
    if mode == "ltx2_i2v":
        return build_ltx2_i2v(**kw)
    if mode == "ltx25_t2v":
        return build_ltx25_t2v(**kw)
    if mode == "ltx25_i2v":
        return build_ltx25_i2v(**kw)
    if mode in ("wan22_t2v", "wan22_t2v_hq", "wan22_t2i", "wan22_t2i_hq"):
        return build_wan22(hq=mode.endswith("_hq"), output=preset["output"], **kw)
    if mode == "wan22_i2i":
        return build_wan22_i2i(**kw)
    if mode in ("wan22_5b_t2v", "wan22_5b_i2v"):
        if mode == "wan22_5b_t2v":
            kw["image"] = None
        return build_wan22_5b(**kw)
    raise ValueError(f"unknown mode {mode}")


def final_size(mode, graph):
    """Final output size: LTX stage-1 latent is half-res; Wan latents are full-res; i2i uses the scaled image."""
    if "latent1" in graph:
        i = graph["latent1"]["inputs"]
        return i["width"] * 2, i["height"] * 2
    if graph["latent"]["class_type"] == "VAEEncode":
        i = graph["img_fit"]["inputs"]
    else:
        i = graph["latent"]["inputs"]
    return i["width"], i["height"]
