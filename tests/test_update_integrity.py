"""The download is checked before it is allowed to overwrite an install.

Pure logic: urlopen is faked, so there is no network here. The failure these
cover is the one that actually happens -- a transfer that stops early, or an
archive that arrives damaged -- not tampering, which is TLS's job.
"""
import io
import zipfile

import pytest

from eve_strait import update


class _FakeResponse:
    """Just enough of urlopen's return value: a context manager that reads."""

    def __init__(self, body: bytes, content_length=None):
        self._buf = io.BytesIO(body)
        length = len(body) if content_length is None else content_length
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._buf.read(n)


@pytest.fixture
def served(monkeypatch):
    """Serve `body` from any URL, optionally lying about Content-Length."""
    def serve(body: bytes, content_length=None):
        monkeypatch.setattr(
            update.urllib.request, "urlopen",
            lambda *a, **k: _FakeResponse(body, content_length))
    return serve


def test_a_complete_download_is_kept(served, tmp_path):
    served(b"x" * 5000)
    out = update.download("https://example/z.zip", dest_dir=tmp_path)
    assert out.read_bytes() == b"x" * 5000


def test_a_truncated_download_is_rejected_and_deleted(served, tmp_path):
    """The regression that matters: a dropped connection does not raise.

    It just stops, leaving a short file that looks like a download. Without
    this check that file goes straight into the install.
    """
    served(b"x" * 400, content_length=5000)

    with pytest.raises(RuntimeError, match="Download incomplete"):
        update.download("https://example/z.zip", dest_dir=tmp_path)

    assert not (tmp_path / "update.zip").exists(), \
        "a partial download must not be left where a later run could use it"


def test_the_release_listing_size_is_a_second_opinion(served, tmp_path):
    """Content-Length agrees with the body, but GitHub said the asset is bigger."""
    served(b"x" * 400)

    with pytest.raises(RuntimeError, match="the release listing said 5,000"):
        update.download("https://example/z.zip", dest_dir=tmp_path,
                        expected_size=5000)


def test_a_download_without_a_content_length_still_checks_the_listing(served, tmp_path):
    served(b"x" * 400, content_length=None)
    update.download("https://example/z.zip", dest_dir=tmp_path, expected_size=400)

    with pytest.raises(RuntimeError):
        update.download("https://example/z.zip", dest_dir=tmp_path,
                        expected_size=999)


def test_an_unknown_size_is_not_treated_as_a_mismatch(served, tmp_path):
    """Zero means "nobody said", not "zero bytes expected"."""
    served(b"x" * 400, content_length=None)
    out = update.download("https://example/z.zip", dest_dir=tmp_path,
                          expected_size=0)
    assert out.read_bytes() == b"x" * 400


# -- archive integrity -----------------------------------------------------

def _zip_with(path, name="eve-strait.exe", body=b"program bytes" * 200):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, body)
    return path


def test_a_good_archive_extracts(tmp_path):
    zip_path = _zip_with(tmp_path / "update.zip")
    out = update._extract(zip_path)
    assert (out / "eve-strait.exe").exists()


def test_a_damaged_archive_is_refused_before_anything_is_written(tmp_path):
    """Flip a byte of compressed data: the entry's own CRC32 no longer matches."""
    zip_path = _zip_with(tmp_path / "update.zip")
    raw = bytearray(zip_path.read_bytes())
    raw[60] ^= 0xFF                      # inside the deflated payload
    zip_path.write_bytes(bytes(raw))

    with pytest.raises(RuntimeError, match="damaged"):
        update._extract(zip_path)

    assert not (tmp_path / "unpacked").exists(), \
        "a damaged archive must not put a single file on disk"


def test_a_file_that_is_not_a_zip_is_refused(tmp_path):
    not_a_zip = tmp_path / "update.zip"
    not_a_zip.write_bytes(b"<html>404 Not Found</html>")

    with pytest.raises(RuntimeError, match="not a readable zip"):
        update._extract(not_a_zip)


def test_an_entry_whose_checksum_fails_is_named(tmp_path):
    """The other shape of damage: the stream unpacks fine, the CRC disagrees.

    Stored (uncompressed) so flipping a byte cannot upset the decompressor --
    this is the branch where testzip() returns a name instead of raising, and
    the message should say which file.
    """
    zip_path = tmp_path / "update.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("eve-strait.exe", b"program bytes" * 200)

    raw = bytearray(zip_path.read_bytes())
    raw[80] ^= 0xFF                      # inside the stored payload
    zip_path.write_bytes(bytes(raw))

    with pytest.raises(RuntimeError, match="eve-strait.exe failed its checksum"):
        update._extract(zip_path)

    assert not (tmp_path / "unpacked").exists()
