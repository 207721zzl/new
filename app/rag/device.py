"""本地 Torch 模型的设备选择策略。"""

from app.logging_config import get_logger


logger = get_logger("rag.device")


def resolve_torch_device(configured_device: str) -> str:
    """解析 cpu/cuda/auto，并在 CUDA 不可用时安全回退到 CPU。"""
    import torch

    cuda_available = torch.cuda.is_available()
    if configured_device == "auto":
        resolved = "cuda" if cuda_available else "cpu"
        logger.info("model device auto-detected device=%s", resolved)
        return resolved
    if configured_device == "cuda" and not cuda_available:
        logger.warning("CUDA was requested but is unavailable; falling back to CPU")
        return "cpu"
    return configured_device
