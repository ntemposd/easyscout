from concurrent.futures import ThreadPoolExecutor

# Global background executor for lightweight offloadable tasks
_EXECUTOR = ThreadPoolExecutor(max_workers=4)


def submit(fn, *args, **kwargs):
    """Submit a function to run in the background. Returns a Future."""
    return _EXECUTOR.submit(fn, *args, **kwargs)
