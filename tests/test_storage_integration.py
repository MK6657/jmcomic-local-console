"""Archive and HTTP consumers share the storage publication contract."""
import zipfile


def test_archives_ignore_reserved_and_legacy_unpublished_pages(tmp_path):
    from core import archive_pages
    from core.packer import CbzPacker
    archive = tmp_path / "source.cbz"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("ch/00001.webp", b"published page")
        output.writestr("ch/00002.abcd1234.webp", b"legacy unfinished page")
        output.writestr("ch/.jmcomic-staging/attempt-1/page.webp", b"unfinished page")
        output.writestr("ch/00003.foo.webp", b"normal dotted name")
    index = archive_pages.read_index(archive)
    assert [page.name for page in index.pages] == ["ch/00001.webp", "ch/00003.foo.webp"]
    assert index.skipped == 0  # Private staging is not a missing/corrupt public page.
    folder = tmp_path / "album"
    folder.mkdir()
    target = folder / "repacked.cbz"
    CbzPacker().pack(folder, target, base=index)
    repacked = archive_pages.read_index(target)
    assert [page.name for page in repacked.pages] == [page.name for page in index.pages]
