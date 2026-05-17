"""
冬 全隐藏版 - 静默启动，无窗口
模块化架构入口
"""
import asyncio
from dong import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        from dong import log
        log("用户退出")
    except Exception as e:
        from dong import log
        import traceback
        log(f"!!! 主进程崩溃: {e}")
        log(traceback.format_exc())
        # 写崩溃日志
        with open("dong_crash.log", "a", encoding="utf-8") as f:
            f.write(f"\n{'='*40}\n")
            f.write(f"崩溃时间: {__import__('datetime').datetime.now()}\n")
            f.write(traceback.format_exc())
        raise
