import re
from dataclasses import dataclass

from ehtg.domain.errors import AppError

_RANGE = re.compile(r"([1-9][0-9]{0,8})(?:\s*-\s*([1-9][0-9]{0,8}))?\Z")


@dataclass(frozen=True)
class PageSelection:
    include: str = ""
    exclude: str = ""

    def __post_init__(self) -> None:
        self._ranges(self.include)
        self._ranges(self.exclude)

    @staticmethod
    def _ranges(expression: str) -> list[tuple[int, int]]:
        if len(expression) > 4096:
            raise AppError("RESOURCE_LIMIT", "Слишком длинное выражение отбора страниц.")
        if not expression.strip():
            return []
        result = []
        for item in expression.split(","):
            match = _RANGE.fullmatch(item.strip())
            if match is None:
                raise AppError("INVALID_SELECTION", "Укажите страницы в формате 1-20,25,30-40.")
            start, end = int(match[1]), int(match[2] or match[1])
            if end < start:
                raise AppError("INVALID_SELECTION", "Начало диапазона больше его конца.")
            result.append((start, end))
        return result

    def resolve(self, page_count: int, *, max_pages: int = 2000) -> tuple[int, ...]:
        if not 1 <= page_count <= max_pages:
            raise AppError("RESOURCE_LIMIT", "Число страниц превышает настроенный предел.")

        def expand(expression: str) -> set[int]:
            pages: set[int] = set()
            for start, end in self._ranges(expression):
                if end > page_count:
                    raise AppError(
                        "PAGE_OUT_OF_RANGE", "Номер страницы выходит за границы галереи."
                    )
                pages.update(range(start, end + 1))
            return pages

        included = expand(self.include) if self.include.strip() else set(range(1, page_count + 1))
        selected = tuple(sorted(included - expand(self.exclude)))
        if not selected:
            raise AppError("EMPTY_SELECTION", "Отбор не должен быть пустым.")
        return selected
