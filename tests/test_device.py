"""Torch 推理设备解析与 CUDA 回退测试。"""

import sys
from types import SimpleNamespace

from app.rag.device import resolve_torch_device


def test_requested_cuda_falls_back_when_torch_has_no_cuda(monkeypatch):
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    assert resolve_torch_device("cuda") == "cpu"
    assert resolve_torch_device("auto") == "cpu"
