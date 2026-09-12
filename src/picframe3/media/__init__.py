"""Media: metadata, image preparation, mats, overlays and video.

Note the deliberate naming: the *module* ``picframe3.media.prepare`` is not
re-exported under its own name, because a package attribute that shadows a
submodule makes ``from picframe3.media import prepare`` mean two different
things depending on import order.  The function is exported as
``prepare_image``.
"""

from .loader import PreparedSlide, SlideLoader
from .mat import MatStyle
from .metadata import PhotoMeta, is_supported, is_video
from .metadata import read as read_metadata
from .prepare import NO_FILES_FILE, PrepareOptions, no_files_screen, placeholder
from .prepare import prepare as prepare_image

__all__ = [
    "PhotoMeta", "read_metadata", "is_supported", "is_video",
    "PrepareOptions", "prepare_image", "placeholder",
    "no_files_screen", "NO_FILES_FILE",
    "PreparedSlide", "SlideLoader", "MatStyle",
]
