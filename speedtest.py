"""Замер скорости интернета по скачиванию файла.

Скрипт последовательно отправляет N GET-запросов на указанный URL, каждый раз
полностью скачивает ответ и печатает среднее время запроса, объём скачанных
данных и среднюю скорость в Мбит/с.

Пример:
    python speedtest.py https://example.com/big-image.jpg
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass

DEFAULT_REQUESTS = 10
DEFAULT_TIMEOUT = 30.0
CHUNK_SIZE = 64 * 1024
BYTES_IN_MB = 1_000_000  # десятичные мегабайты, как у провайдеров и speedtest-сервисов
USER_AGENT = "internet-speed-meter/1.0 (+https://github.com/r-1805/internet-speed-meter)"

EXIT_OK = 0
EXIT_ALL_FAILED = 1
EXIT_INTERRUPTED = 130


@dataclass
class RequestResult:
    """Результат одного запроса."""

    number: int
    elapsed: float  # полное время запроса: соединение + заголовки + всё тело, с
    ttfb: float | None  # время до получения заголовков ответа, с
    size: int  # скачано байт тела ответа
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Summary:
    """Итоговая статистика по успешным запросам."""

    total: int
    succeeded: int
    total_bytes: int
    total_time: float
    avg_time: float
    avg_ttfb: float
    speed_mbit_s: float
    speed_mb_s: float


def measure_request(url: str, timeout: float, number: int = 1) -> RequestResult:
    """Выполняет один GET-запрос и скачивает тело ответа до конца."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            # Просим прокси и CDN не отдавать ответ из кэша, иначе замер покажет
            # скорость кэша, а не канала.
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    size = 0
    ttfb: float | None = None
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ttfb = time.perf_counter() - start
            while chunk := response.read(CHUNK_SIZE):
                size += len(chunk)
            elapsed = time.perf_counter() - start
            expected = response.headers.get("Content-Length", "")
    except (OSError, http.client.HTTPException) as exc:
        return RequestResult(
            number=number,
            elapsed=time.perf_counter() - start,
            ttfb=ttfb,
            size=size,
            error=describe_error(exc),
        )
    # При чтении кусками http.client не бросает IncompleteRead, если сервер
    # закрыл соединение раньше времени, поэтому сверяем размер сами.
    error = None
    if expected.isdigit() and size < int(expected):
        error = f"соединение оборвалось: получено {size} из {expected} байт"
    return RequestResult(number=number, elapsed=elapsed, ttfb=ttfb, size=size, error=error)


def describe_error(exc: BaseException) -> str:
    """Превращает исключение сети в короткое понятное сообщение."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        # urllib заворачивает настоящую причину (DNS, тайм-аут, отказ) в URLError.
        if isinstance(exc.reason, BaseException):
            return describe_error(exc.reason)
        return f"ошибка соединения: {exc.reason}"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "тайм-аут"
    if isinstance(exc, socket.gaierror):
        return f"не удалось найти хост (DNS): {exc}"
    if isinstance(exc, http.client.IncompleteRead):
        return "соединение оборвалось до конца ответа"
    return f"{type(exc).__name__}: {exc}"


def run(
    url: str,
    count: int,
    timeout: float,
    on_result: Callable[[RequestResult], None] | None = None,
) -> list[RequestResult]:
    """Последовательно выполняет count запросов."""
    results = []
    for number in range(1, count + 1):
        result = measure_request(url, timeout, number)
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


def summarize(results: Sequence[RequestResult]) -> Summary:
    """Считает статистику только по успешным запросам.

    Средняя скорость = суммарные байты / суммарное время. Среднее скоростей
    отдельных запросов не используется: один короткий быстрый запрос завышал
    бы итог.
    """
    ok = [r for r in results if r.ok]
    total_bytes = sum(r.size for r in ok)
    total_time = sum(r.elapsed for r in ok)
    bytes_per_second = total_bytes / total_time if total_time > 0 else 0.0
    return Summary(
        total=len(results),
        succeeded=len(ok),
        total_bytes=total_bytes,
        total_time=total_time,
        avg_time=total_time / len(ok) if ok else 0.0,
        avg_ttfb=sum(r.ttfb or 0.0 for r in ok) / len(ok) if ok else 0.0,
        speed_mbit_s=bytes_per_second * 8 / BYTES_IN_MB,
        speed_mb_s=bytes_per_second / BYTES_IN_MB,
    )


def format_result(result: RequestResult, total: int) -> str:
    width = len(str(total))
    prefix = f"[{result.number:>{width}}/{total}]"
    if not result.ok:
        return f"{prefix}  ОШИБКА: {result.error} (через {result.elapsed:.3f} с)"
    speed = result.size * 8 / BYTES_IN_MB / result.elapsed if result.elapsed > 0 else 0.0
    return (
        f"{prefix}  {result.elapsed:7.3f} с"
        f"  (ответ через {result.ttfb or 0.0:.3f} с)"
        f"  {result.size / BYTES_IN_MB:8.2f} МБ"
        f"  {speed:8.2f} Мбит/с"
    )


def format_summary(summary: Summary) -> str:
    lines = [
        "-" * 60,
        f"Успешных запросов:     {summary.succeeded}/{summary.total}",
    ]
    if summary.succeeded:
        lines += [
            f"Скачано всего:         {summary.total_bytes / BYTES_IN_MB:.2f} МБ"
            f" ({summary.total_bytes} байт)",
            f"Среднее время запроса: {summary.avg_time:.3f} с",
            f"Среднее время ответа:  {summary.avg_ttfb:.3f} с",
            f"Средняя скорость:      {summary.speed_mbit_s:.2f} Мбит/с"
            f" ({summary.speed_mb_s:.2f} МБ/с)",
        ]
    else:
        lines.append("Ни один запрос не выполнился, скорость посчитать нельзя.")
    return "\n".join(lines)


def round_floats(data: dict[str, object], digits: int = 6) -> dict[str, object]:
    return {k: round(v, digits) if isinstance(v, float) else v for k, v in data.items()}


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("должно быть целым числом >= 1")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("должно быть числом > 0")
    return number


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Замер скорости интернета: N последовательных скачиваний файла по URL.",
    )
    parser.add_argument("url", help="адрес тяжёлого файла, например большой картинки")
    parser.add_argument(
        "-n",
        "--requests",
        type=positive_int,
        default=DEFAULT_REQUESTS,
        help=f"сколько запросов выполнить (по умолчанию {DEFAULT_REQUESTS})",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT,
        help=f"тайм-аут ожидания данных от сервера, с (по умолчанию {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="вывести результат в JSON вместо текста",
    )
    args = parser.parse_args(argv)
    if not args.url.lower().startswith(("http://", "https://")):
        parser.error("URL должен начинаться с http:// или https://")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    def print_progress(result: RequestResult) -> None:
        print(format_result(result, args.requests), flush=True)

    try:
        if not args.json:
            print(f"Замер скорости: {args.url}")
            print(f"Запросов: {args.requests}, тайм-аут: {args.timeout:g} с\n", flush=True)
        results = run(
            args.url,
            args.requests,
            args.timeout,
            on_result=None if args.json else print_progress,
        )
    except KeyboardInterrupt:
        print("\nПрервано пользователем.", file=sys.stderr)
        return EXIT_INTERRUPTED

    summary = summarize(results)
    if args.json:
        payload = {
            "url": args.url,
            "requests": [round_floats(asdict(r)) for r in results],
            "summary": round_floats(asdict(summary)),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_summary(summary))
    return EXIT_OK if summary.succeeded else EXIT_ALL_FAILED


if __name__ == "__main__":
    sys.exit(main())
