"""Single shared ThreadPoolExecutor for this app's off-request background
work (EPUB import, AI summary/retrospective generation, ...). See
epub_import_jobs.py's module docstring for why a ThreadPoolExecutor is used
at all (no task queue in this codebase) and why jobs must only ever be
submitted via transaction.on_commit(...) (ATOMIC_REQUESTS=True).

A single shared pool avoids doubling worker threads as more background job
types get added — each job type still gets its own module for the actual
work (epub_import_jobs.py, ai_generation_jobs.py, ...), just not its own
executor.
"""
from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=2)
