import shutil
from pathlib import Path


def _deploy_model(ctx) -> None:
    project_root = Path(__file__).resolve().parents[1]
    source_zip = project_root / "models" / "vosk-model-small-en-us.zip"
    if not source_zip.exists():
        return
    assets_dir = Path(ctx.bootstrap.build_dir) / "assets" / "private" / "models"
    assets_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_zip, assets_dir / source_zip.name)


def before_build(*args, **kwargs) -> None:  # pragma: no cover - build pipeline hook
    ctx = kwargs.get("ctx")
    if ctx is None and args:
        ctx = args[0]
    if ctx is None:
        return
    _deploy_model(ctx)


def after_build(*args, **kwargs) -> None:  # pragma: no cover - build pipeline hook
    ctx = kwargs.get("ctx")
    if ctx is None and args:
        ctx = args[0]
    if ctx is None:
        return
    _deploy_model(ctx)
