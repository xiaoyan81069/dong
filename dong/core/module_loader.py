"""冬 · 模块自动发现与加载 — import 即注册 @bus.on_phase"""
import importlib, logging, os

__all__ = ["discover_and_load"]

logger = logging.getLogger("dong.core.module_loader")

def discover_and_load(package: str):
    """扫描包目录,import 所有 .py 模块及其子包(触发 @bus.on_phase 注册)。"""
    pkg = importlib.import_module(package)
    pkg_dir = os.path.dirname(pkg.__file__ or ".")
    for fname in sorted(os.listdir(pkg_dir)):
        full_path = os.path.join(pkg_dir, fname)
        if fname.startswith("_") or fname.startswith("."):
            continue
        if os.path.isdir(full_path):
            # 递归加载子包
            init_file = os.path.join(full_path, "__init__.py")
            if os.path.exists(init_file):
                try:
                    importlib.import_module(f"{package}.{fname}")
                except Exception as exc:
                    logger.warning("子包加载跳过 %s: %s", fname, exc)
        elif fname.endswith(".py"):
            mod_name = f"{package}.{fname[:-3]}"
            try:
                importlib.import_module(mod_name)
            except Exception as exc:
                logger.warning("模块加载跳过 %s: %s", mod_name, exc)
