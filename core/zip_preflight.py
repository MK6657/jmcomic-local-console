"""Bound the ZIP directory before allocating the full member list."""

import zipfile


class ZipMetadataLimit(zipfile.BadZipFile):
    """The declared directory size or actual number of entries exceeds the limit."""


class BoundedZipFile(zipfile.ZipFile):
    def __init__(self, handle, *, max_entries, max_directory_bytes, check=None):
        self._entry_limit = max_entries
        self._directory_limit = max_directory_bytes
        self._check = check
        super().__init__(handle)

    def _RealGetContents(self):
        """Use the stdlib's ZIP64 and prefixed-file handling with bounded allocation."""
        limit, check = self._entry_limit, self._check
        end = zipfile._EndRecData(self.fp)
        if check is not None:
            check()
        if end and (end[zipfile._ECD_ENTRIES_TOTAL] > limit
                    or end[zipfile._ECD_SIZE] > self._directory_limit):
            raise ZipMetadataLimit("压缩包目录条目或元数据超过上限")

        class BoundedEntries(list):
            def append(self, info):
                if check is not None:
                    check()
                if len(self) >= limit:
                    raise ZipMetadataLimit("压缩包目录条目或元数据超过上限")
                super().append(info)

        self.filelist = BoundedEntries()
        super()._RealGetContents()
