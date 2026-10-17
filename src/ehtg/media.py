"""Bounded media decoding. Invoked only in the resource-limited child process."""

import hashlib
import io
import os
import warnings
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from ehtg.domain.errors import AppError


def media_error(code: str = "MEDIA_INVALID") -> AppError:
    messages = {
        "MEDIA_INVALID": "Файл не является целым поддерживаемым изображением.",
        "ANIMATION_REVIEW": "Обнаружена анимация. Выберите первый кадр или исключите страницу.",
        "MEDIA_UNREADABLE": "Не удалось подготовить читаемое фото в пределах Telegram.",
    }
    return AppError(code, messages[code])


def describe(path: Path, width: int, height: int, transforms: list[str]) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
            size += len(chunk)
    return {
        "sha256": digest.hexdigest(),
        "size": size,
        "width": width,
        "height": height,
        "transforms": transforms,
    }


def prepare(
    source: Path, destination: Path, *, max_pixels: int = 40_000_000, first_frame: bool = False
) -> dict[str, Any]:
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as check:
                if check.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                    raise media_error()
                if check.width * check.height > max_pixels:
                    raise media_error()
                check.verify()
            with Image.open(source) as opened:
                animated = getattr(opened, "n_frames", 1) > 1
                if opened.format == "GIF" and not animated:
                    raise media_error()
                # Write the preview before reporting animation; never send it without a decision.
                image = ImageOps.exif_transpose(opened).convert("RGBA")
                width, height = image.size
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                transforms = ["exif_orientation", "white_alpha", "jpeg"]
                long, short = max(background.size), min(background.size)
                if long > short * 20:
                    padded = (
                        (background.width, max(background.height, (long + 19) // 20))
                        if background.width == long
                        else (max(background.width, (long + 19) // 20), background.height)
                    )
                    # Center explicitly; do not crop or scale the page while adding fields.
                    canvas = Image.new("RGB", padded, "white")
                    original = Image.new("RGB", image.size, "white")
                    original.paste(image, mask=image.getchannel("A"))
                    canvas.paste(original, ((padded[0] - width) // 2, (padded[1] - height) // 2))
                    background = canvas
                    transforms.append("aspect_padding")
                scale = min(1.0, 10000 / sum(background.size), 4096 / max(background.size))
                if scale < 1:
                    background = background.resize(
                        (
                            max(1, int(background.width * scale)),
                            max(1, int(background.height * scale)),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                    transforms.append("resize")
                long, short = max(background.size), min(background.size)
                if long > short * 20:
                    dimensions = (
                        (max(background.width, (long + 19) // 20), background.height)
                        if background.height == long
                        else (background.width, max(background.height, (long + 19) // 20))
                    )
                    canvas = Image.new("RGB", dimensions, "white")
                    canvas.paste(
                        background,
                        (
                            (dimensions[0] - background.width) // 2,
                            (dimensions[1] - background.height) // 2,
                        ),
                    )
                    background = canvas
                # Preserve at least a 1280px long edge when the input was that large.
                for _ in range(5):
                    for quality in (92, 85, 75):
                        encoded = io.BytesIO()
                        background.save(encoded, format="JPEG", quality=quality, optimize=True)
                        if encoded.tell() <= 9_500_000:
                            temporary = destination.with_suffix(".tmp")
                            with temporary.open("wb") as handle:
                                handle.write(encoded.getbuffer())
                                handle.flush()
                                os.fsync(handle.fileno())
                            os.replace(temporary, destination)
                            if animated and not first_frame:
                                raise media_error("ANIMATION_REVIEW")
                            return {
                                "source": describe(source, width, height, []),
                                "photo": describe(destination, *background.size, transforms),
                            }
                    if max(background.size) * 0.8 < min(1280, max(image.size)):
                        break
                    background = background.resize(
                        (max(1, int(background.width * 0.8)), max(1, int(background.height * 0.8))),
                        Image.Resampling.LANCZOS,
                    )
                raise media_error("MEDIA_UNREADABLE")
    except AppError:
        raise
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise media_error() from None
