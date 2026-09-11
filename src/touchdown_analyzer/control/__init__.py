"""Browser control for the capture side.

Split in two so the dependency stays out of the recorder: :mod:`service` owns
the recording thread and is stdlib-only, :mod:`app` is the FastAPI layer on
top. If uvicorn will not start, ``touchdown-analyzer record`` still works.
"""
