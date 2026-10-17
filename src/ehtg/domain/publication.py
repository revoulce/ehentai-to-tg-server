import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ehtg.domain.errors import AppError


@dataclass(frozen=True)
class PublicationOptions:
    title: str | None = None
    caption: str | None = None
    removed_tags: list[str] = field(default_factory=list)
    disable_notification: bool = True
    has_spoiler: bool = False


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def page_ranges(pages: list[int]) -> str:
    runs: list[str] = []
    start = end = pages[0]
    for page in [*pages[1:], -1]:
        if page == end + 1:
            end = page
            continue
        runs.append(str(start) if start == end else f"{start}–{end}")
        start = end = page
    return ", ".join(runs)


def hashtag(value: str) -> str:
    text = re.sub(r"[^\w]", "", re.sub(r"[\s\-/]+", "_", value), flags=re.UNICODE)
    return "#" + text if text else ""


def publication_plan(
    metadata: dict[str, Any], source_url: str, pages: list[int], options: PublicationOptions
) -> dict[str, Any]:
    if not pages:
        raise AppError("EMPTY_SELECTION", "Отбор не должен быть пустым.")
    title = (options.title if options.title is not None else metadata["title"]).strip()
    if not title:
        raise AppError("INVALID_CAPTION", "Название не должно быть пустым.")
    tags = list(
        dict.fromkeys(
            hashtag(name)
            for namespace, name in metadata["tags"]
            if f"{namespace}:{name}" not in options.removed_tags and hashtag(name)
        )
    )
    body: list[str] = []
    if options.caption is not None:
        if options.caption.strip():
            body.append(options.caption.strip())
    else:
        authors = " / ".join([*metadata.get("authors", []), *metadata.get("groups", [])])
        if authors:
            body.append(f"Автор / группа: {authors}")
        if metadata.get("language"):
            body.append(f"Язык: {metadata['language']}")
        if metadata.get("category"):
            body.append(f"Категория: {metadata['category']}")
    body.append(f"Страниц: {len(pages)} из {len(metadata['page_ids'])}")
    chunks = [pages[index : index + 10] for index in range(0, len(pages), 10)]
    parts = []
    hidden = 0
    for index, chunk in enumerate(chunks, 1):
        suffix = f"Часть {index}/{len(chunks)} · страницы {page_ranges(chunk)}"
        if index == 1:

            def render(visible: list[str], suffix: str = suffix) -> str:
                return "\n".join(
                    [
                        title,
                        *body,
                        *([" ".join(visible)] if visible else []),
                        f"Источник: {source_url}",
                        suffix,
                    ]
                )

            visible = tags.copy()
            while visible and units(render(visible)) > 1024:
                visible.pop()
            caption = render(visible)
            hidden = len(tags) - len(visible)
        else:
            caption = f"{title}\n{suffix}"
        if units(caption) > 1024:
            raise AppError(
                "CAPTION_TOO_LONG",
                "Название и обязательные поля не помещаются. "
                "Сократите подпись вручную перед подтверждением.",
            )
        parts.append({"index": index, "pages": chunk, "caption": caption})
    return {"parts": parts, "hidden_tags": hidden, "options": asdict(options)}
