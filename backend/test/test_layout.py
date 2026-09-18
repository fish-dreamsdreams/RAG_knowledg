"""布局约定：测试代码不得出现在 app/ 内（tasklist 1.6）。"""

from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
TEST_DIR = Path(__file__).resolve().parent


def test_no_tests_inside_app() -> None:
    assert not list(APP_DIR.rglob("test_*.py")), "app/ 内不允许出现测试文件"
    assert not list(APP_DIR.rglob("tests")), "app/ 内不允许出现 tests 目录"


def test_test_dir_is_inside_backend() -> None:
    assert TEST_DIR.name == "test"
    assert (TEST_DIR.parent / "app").is_dir()


def test_test_dir_mirrors_app_layers() -> None:
    for layer in ("api", "engines", "graphs", "services", "integration"):
        assert (TEST_DIR / layer).exists(), f"test/{layer} 应存在（镜像 app/ 结构）"
