from setuptools import setup, Extension

try:
    from Cython.Build import cythonize
    ext_modules = cythonize(
        [Extension("rdpy.core.rle", ["rdpy/core/rle.pyx"])],
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
        },
    )
except ImportError:
    ext_modules = []

setup(ext_modules=ext_modules)
