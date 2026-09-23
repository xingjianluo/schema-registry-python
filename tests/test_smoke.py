def test_imports() -> None:
    import app.api  # noqa: F401
    import app.checker  # noqa: F401
    import app.registry  # noqa: F401
    import app.store  # noqa: F401
    from app.models import CompatibilityLevel, IncompatibilityCode

    assert CompatibilityLevel.BACKWARD.value == "BACKWARD"
    assert IncompatibilityCode.FIELD_REMOVED.value == "FIELD_REMOVED"
