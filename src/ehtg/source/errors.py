from ehtg.domain.errors import AppError

_ERRORS = {
    "MEDIA_INVALID": ("Файл изображения повреждён или имеет неподдерживаемый формат.", False),
    "MEDIA_UNREADABLE": ("Невозможно подготовить читаемое фото в пределах Telegram.", False),
    "ANIMATION_REVIEW": ("Анимация требует выбора первого кадра или исключения страницы.", False),
    "MEDIA_URL_EXPIRED": ("Адрес изображения истёк. Требуется новое разрешение страницы.", True),
    "DISK_FULL": ("Недостаточно свободного места для загрузки.", False),
    "SOURCE_AUTH_REQUIRED": ("Требуется действующая авторизованная сессия E-Hentai.", False),
    "SOURCE_QUOTA_EXCEEDED": ("Квота источника исчерпана.", False),
    "SOURCE_TEMPORARILY_BLOCKED": ("Источник временно ограничил доступ.", False),
    "SOURCE_NETWORK_UNAVAILABLE": ("Не удалось связаться с источником.", True),
    "SOURCE_NETWORK_POLICY": ("Источник перенаправляет запрос за разрешённые границы сети.", False),
    "SOURCE_VERSION_CHANGED": ("Состав галереи изменился во время анализа.", False),
    "GALLERY_UNAVAILABLE": ("Галерея недоступна.", False),
    "EXTRACTOR_INCOMPATIBLE": ("Парсер источника несовместим с полученными данными.", False),
    "RESOURCE_LIMIT": ("Анализ превысил настроенный предел ресурсов.", False),
    "INVALID_COOKIE_FILE": ("Нужен корректный Netscape cookie jar для E-Hentai.", False),
}


class SourceError(AppError):
    def __init__(self, code: str) -> None:
        safe_code = code if code in _ERRORS else "EXTRACTOR_INCOMPATIBLE"
        message, retryable = _ERRORS[safe_code]
        super().__init__(safe_code, message, 503, retryable=retryable)
