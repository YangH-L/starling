import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.editable_wheel import editable_wheel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_info_hook import write_build_info  # noqa: E402


class _BuildPy(build_py):
    def run(self):
        write_build_info()
        super().run()


class _Editable(editable_wheel):
    def run(self):
        write_build_info()
        super().run()


setup(cmdclass={"build_py": _BuildPy, "editable_wheel": _Editable})
