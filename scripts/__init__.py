"""APIx package marker.

Deliberately not empty: GitHub's web uploader silently skips zero-byte
files, which drops the package markers and makes every import fail in CI
with ModuleNotFoundError. A comment costs nothing and survives the upload.
"""
