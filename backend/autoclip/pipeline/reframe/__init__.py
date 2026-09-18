"""Static reframing helpers.

AutoClip's automatic Reframe stage intentionally uses one centered crop per
clip. Subject-specific composition belongs to the manual Layout editor.

The crop-path types retain support for older tracked project files so those
projects remain previewable and exportable.
"""

from .croppath import CropPath, CropSegment, Strategy, centre_crop

__all__ = ["CropPath", "CropSegment", "Strategy", "centre_crop"]
