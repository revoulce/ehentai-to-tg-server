from pathlib import Path

import pytest
from PIL import Image

from ehtg.artifacts import ArtifactStore
from ehtg.domain.errors import AppError
from ehtg.domain.selection import PageSelection
from ehtg.domain.snapshot import GallerySnapshot
from ehtg.media import prepare
from ehtg.service import GalleryService
from ehtg.settings import Settings
from ehtg.source.media_transport import media_target
from ehtg.source.process import IsolatedProcess


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP"])
def test_real_isolated_preparation(format: str, tmp_path: Path) -> None:
    source, photo = tmp_path / "source", tmp_path / "photo"
    Image.new("RGB", (300, 100), "red").save(source, format=format)
    result = IsolatedProcess(timeout=20).run(
        {
            "operation": "prepare",
            "source_path": str(source),
            "photo_path": str(photo),
            "max_pixels": 1_000_000,
            "first_frame": False,
            "memory_mb": 256,
        },
        lambda: None,
    )
    assert result["source"]["width"] == 300
    assert result["photo"]["size"] == photo.stat().st_size
    with Image.open(photo) as image:
        assert image.format == "JPEG" and image.size == (300, 100)


def test_animation_stops_with_preview_and_requires_decision(tmp_path: Path) -> None:
    source, photo = tmp_path / "source", tmp_path / "photo"
    Image.new("RGB", (100, 100), "red").save(
        source,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (100, 100), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(AppError) as error:
        prepare(source, photo)
    assert error.value.code == "ANIMATION_REVIEW"
    assert photo.is_file()
    assert prepare(source, photo, first_frame=True)["photo"]["width"] == 100


def test_bad_images_and_extreme_geometry(tmp_path: Path) -> None:
    source, photo = tmp_path / "source", tmp_path / "photo"
    source.write_text("<html>login</html>")
    with pytest.raises(AppError):
        prepare(source, photo)
    Image.new("RGB", (50, 5000), "red").save(source, format="PNG")
    result = prepare(source, photo)
    assert result["photo"]["height"] / result["photo"]["width"] <= 20
    assert "aspect_padding" in result["photo"]["transforms"]


@pytest.mark.parametrize(
    "url",
    [
        "http://a.hath.network/image",
        "https://evil.test/image",
        "https://e-hentai.org.evil.test/image",
        "https://a.hath.network:22/image",
        "https://secret@a.hath.network/image",
        "https://a.hath.network/img/509.gif",
        "https://127.0.0.1/image",
        "https://a.hath.network/image\r\nCookie: secret",
    ],
)
def test_media_network_policy(url: str) -> None:
    with pytest.raises(AppError):
        media_target(url)


def test_artifacts_recheck_disk_and_hash(service: GalleryService, tmp_path: Path) -> None:
    draft = service.create_draft("https://e-hentai.org/g/123/abcdef0123/", PageSelection())
    service.record_analysis(draft["id"], 1, GallerySnapshot("test", ("one",)))
    job_id = service.publish(draft["id"], 2, "media")["job_id"]
    store = ArtifactStore(
        Settings(
            _env_file=None,
            owner_id=service.owner_id,
            channel_id=service.channel_id,
            files_path=tmp_path / "files",
        ),
        service.db,
    )
    source, photo = store.path(job_id, 1, "source"), store.path(job_id, 1, "photo")
    Image.new("RGB", (100, 100), "red").save(source, format="PNG")
    store.save(job_id, 1, prepare(source, photo))
    store.manifest(job_id)
    assert store.valid(job_id, 1, "source") and store.valid(job_id, 1, "photo")
    photo.write_bytes(b"corrupt")
    assert not store.valid(job_id, 1, "photo")
    assert (source.parent / "manifest.json").is_file()
