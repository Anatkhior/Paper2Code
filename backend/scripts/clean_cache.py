"""Delete only reproducible PDF caches; saved runs and their repositories survive."""
import shutil

from app.config import settings


def clean_cache() -> int:
    count = 0
    if not settings.data_dir.exists():
        return count
    for directory in settings.data_dir.iterdir():
        if directory.is_symlink() or not (directory / "meta.json").is_file():
            continue
        cache = directory / "cache"
        if cache.is_dir() and not cache.is_symlink():
            shutil.rmtree(cache)
            count += 1
    return count


if __name__ == "__main__":
    print(f"已清理 {clean_cache()} 份可再生 PDF 缓存；历史解读、论文、对话和代码仓库均保留。")
