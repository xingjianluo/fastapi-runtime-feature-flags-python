def test_skeleton_imports():
    import app.api  # noqa: F401
    import app.config.loader  # noqa: F401
    import app.flags.evaluator  # noqa: F401
    import app.runtime.store  # noqa: F401
