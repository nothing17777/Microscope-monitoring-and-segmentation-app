"""Device selection, shared by the worker and the spec dump."""


def pick_device() -> str:
    """cuda if available, else mps, else cpu."""
    try:
        import torch
    except Exception:  # noqa: BLE001 - torch absent: dashboard still runs
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"
