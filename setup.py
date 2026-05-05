from setuptools import setup, Extension
import os

if os.environ.get('NO_CYTHON'):
    ext_modules = []
else:
    try:
        from Cython.Build import cythonize
        ext_modules = cythonize(
            [
                Extension("rdpy.core.rle", ["rdpy/core/rle.pyx"]),
                Extension(
                    "rdpy.protocol.rdp.rlgr1_decode",
                    ["rdpy/protocol/rdp/rlgr1_decode.pyx"],
                    include_dirs=[__import__('numpy').get_include()],
                ),
                Extension(
                    "rdpy.protocol.rdp.zgfx",
                    ["rdpy/protocol/rdp/zgfx.pyx"],
                ),
            ],
            compiler_directives={
                "language_level": "3",
                "boundscheck": False,
                "wraparound": False,
            },
        )
    except ImportError:
        ext_modules = []

setup(ext_modules=ext_modules)
