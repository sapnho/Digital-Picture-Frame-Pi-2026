"""ctypes binding for GBM (Generic Buffer Management).

GBM is the allocator that hands EGL buffers which the DRM scanout engine can
display directly.  It is the piece that lets a plain Python process render with
the GPU straight onto a monitor with nothing else running.
"""

from __future__ import annotations

import ctypes
import ctypes.util

lib = ctypes.CDLL(ctypes.util.find_library("gbm") or "libgbm.so.1")


def fourcc(a: str, b: str, c: str, d: str) -> int:
    return ord(a) | (ord(b) << 8) | (ord(c) << 16) | (ord(d) << 24)


FORMAT_XRGB8888 = fourcc("X", "R", "2", "4")
FORMAT_ARGB8888 = fourcc("A", "R", "2", "4")
FORMAT_XBGR8888 = fourcc("X", "B", "2", "4")
FORMAT_RGB565 = fourcc("R", "G", "1", "6")

USE_SCANOUT = 1 << 0
USE_CURSOR = 1 << 1
USE_RENDERING = 1 << 2
USE_WRITE = 1 << 3
USE_LINEAR = 1 << 4

FORMAT_NAMES = {
    FORMAT_XRGB8888: "XRGB8888",
    FORMAT_ARGB8888: "ARGB8888",
    FORMAT_XBGR8888: "XBGR8888",
    FORMAT_RGB565: "RGB565",
}


class BoHandle(ctypes.Union):
    _fields_ = [
        ("ptr", ctypes.c_void_p),
        ("s32", ctypes.c_int32),
        ("u32", ctypes.c_uint32),
        ("s64", ctypes.c_int64),
        ("u64", ctypes.c_uint64),
    ]


lib.gbm_create_device.restype = ctypes.c_void_p
lib.gbm_create_device.argtypes = [ctypes.c_int]
lib.gbm_device_destroy.argtypes = [ctypes.c_void_p]
lib.gbm_device_is_format_supported.restype = ctypes.c_int
lib.gbm_device_is_format_supported.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
lib.gbm_surface_create.restype = ctypes.c_void_p
lib.gbm_surface_create.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.c_uint32, ctypes.c_uint32]
lib.gbm_surface_destroy.argtypes = [ctypes.c_void_p]
lib.gbm_surface_lock_front_buffer.restype = ctypes.c_void_p
lib.gbm_surface_lock_front_buffer.argtypes = [ctypes.c_void_p]
lib.gbm_surface_release_buffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
lib.gbm_surface_has_free_buffers.restype = ctypes.c_int
lib.gbm_surface_has_free_buffers.argtypes = [ctypes.c_void_p]
lib.gbm_bo_get_width.restype = ctypes.c_uint32
lib.gbm_bo_get_width.argtypes = [ctypes.c_void_p]
lib.gbm_bo_get_height.restype = ctypes.c_uint32
lib.gbm_bo_get_height.argtypes = [ctypes.c_void_p]
lib.gbm_bo_get_stride.restype = ctypes.c_uint32
lib.gbm_bo_get_stride.argtypes = [ctypes.c_void_p]
lib.gbm_bo_get_format.restype = ctypes.c_uint32
lib.gbm_bo_get_format.argtypes = [ctypes.c_void_p]
lib.gbm_bo_get_handle.restype = BoHandle
lib.gbm_bo_get_handle.argtypes = [ctypes.c_void_p]


def create_device(fd: int) -> ctypes.c_void_p:
    dev = lib.gbm_create_device(fd)
    if not dev:
        raise RuntimeError("gbm_create_device failed")
    return ctypes.c_void_p(dev)


def create_surface(dev, width: int, height: int, fmt: int, flags: int) -> ctypes.c_void_p:
    surf = lib.gbm_surface_create(dev, width, height, fmt, flags)
    if not surf:
        raise RuntimeError(
            f"gbm_surface_create({width}x{height}, {FORMAT_NAMES.get(fmt, hex(fmt))}) failed"
        )
    return ctypes.c_void_p(surf)
