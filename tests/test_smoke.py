import tomllib
from pathlib import Path

PYPROJECT = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())


def test_package_imports():
    import invoice_pipeline

    assert invoice_pipeline.__name__ == "invoice_pipeline"


def test_slow_marker_is_registered(pytestconfig):
    markers = pytestconfig.getini("markers")
    assert any(m.startswith("slow") for m in markers)


def test_python_version_and_tui_extra():
    project = PYPROJECT["project"]
    assert "3.14" in project["requires-python"]
    assert any(d.startswith("textual") for d in project["optional-dependencies"]["tui"])
    core = " ".join(project["dependencies"]).lower()
    assert "textual" not in core


def test_no_image_recognition_dependency():
    project = PYPROJECT["project"]
    everything = project["dependencies"] + [
        d for extra in project["optional-dependencies"].values() for d in extra
    ]
    banned = ("pillow", "tesseract", "easyocr", "opencv", "ocr")
    assert not [d for d in everything if any(b in d.lower() for b in banned)]


def test_pytest_collects_tests_only():
    assert PYPROJECT["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
