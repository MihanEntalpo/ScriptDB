import importlib


def test_all_exports_importable():
    """Every name in ``scriptdb.__all__`` should be importable."""
    scriptdb = importlib.import_module("scriptdb")
    module = __import__("scriptdb", fromlist=scriptdb.__all__)
    for name in scriptdb.__all__:
        assert getattr(module, name)
