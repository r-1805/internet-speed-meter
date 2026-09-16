"""Замер скорости интернета по скачиванию файла.

Скрипт последовательно отправляет N GET-запросов на указанный URL, каждый раз
полностью скачивает ответ и печатает среднее время запроса, объём скачанных
данных и среднюю скорость в Мбит/с.

Пример:
    python speed_meter.py https://example.com/big-image.jpg
"""

import argparse
import errno
import http.client
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from typing import NoReturn

DEFAULT_REQUESTS = 10
DEFAULT_TIMEOUT = 30.0
CHUNK_SIZE = 64 * 1024
BYTES_IN_MB = 1_000_000  # десятичные мегабайты, как у провайдеров
# Меньше этого размера время уходит в основном на соединение, и скорость неточна.
SMALL_RESPONSE_BYTES = BYTES_IN_MB
# Без User-Agent часть CDN (например, upload.wikimedia.org) отвечает 403.
USER_AGENT = "internet-speed-meter (+https://github.com/r-1805/internet-speed-meter)"

EXIT_OK = 0
EXIT_ALL_FAILED = 1
EXIT_INTERRUPTED = 130
EXIT_BROKEN_PIPE = 141  # как у shell-утилит при SIGPIPE

# Один SSL-контекст на все запросы. Без него http.client на каждое соединение
# заново загружает системные сертификаты (~5 мс), и это время попадало бы в замер.
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=ssl.create_default_context())
)


@dataclass
class RequestResult:
    number: int
    elapsed: float  # полное время запроса: соединение, ожидание ответа, скачивание тела, с
    ttfb: float | None  # время до первого байта (заголовков) ответа; None, если ответа не было
    size: int  # скачано байт тела ответа
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Summary:
    """Итог по успешным запросам. Если успешных нет, метрики равны None."""

    total: int
    succeeded: int
    total_bytes: int
    avg_time: float | None
    avg_ttfb: float | None
    download_speed_mbit_s: float | None  # байты / время скачивания тела
    request_speed_mbit_s: float | None  # байты / полное время запроса


# --- Адрес -------------------------------------------------------------------


def normalize_url(url: str) -> str:
    """Проверяет URL и кодирует символы, которые urllib не принимает как есть.

    Адрес, скопированный из браузера, может содержать кириллицу или пробелы:
    `https://ru.wikipedia.org/wiki/Тест`. Без кодирования http.client падает
    с UnicodeEncodeError. Уже закодированные `%XX` не трогаем.
    """
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        raise ValueError(f"некорректный URL: {url}") from None
    try:
        port = parts.port
    except ValueError:
        raise ValueError(f"некорректный порт в URL: {url}") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("URL должен начинаться с http:// или https://")
    if not parts.hostname:
        raise ValueError("в URL не указан хост")

    netloc = parts.netloc
    if not netloc.isascii():
        try:
            host = parts.hostname.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError(f"некорректное доменное имя: {parts.hostname}") from None
        netloc = host if port is None else f"{host}:{port}"
    path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parts.query, safe="/?%:@!$&'()*+,;=-._~")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ""))


# --- Замер -------------------------------------------------------------------


def measure_request(url: str, timeout: float, number: int) -> RequestResult:
    """Выполняет один GET-запрос и скачивает тело ответа до конца."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    # readinto в один буфер вместо read(): read() на каждый кусок создаёт новый bytes,
    # и скрипт сам начинает ограничивать замер на быстрых каналах.
    buffer = memoryview(bytearray(CHUNK_SIZE))
    size = 0
    ttfb = None
    start = time.perf_counter()
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            ttfb = time.perf_counter() - start
            while received := response.readinto(buffer):
                size += received
            elapsed = time.perf_counter() - start
            headers = response.headers
    except (OSError, http.client.HTTPException) as exc:
        return RequestResult(number, time.perf_counter() - start, ttfb, size, describe_error(exc))
    return RequestResult(number, elapsed, ttfb, size, check_complete(headers, size))


def check_complete(headers: http.client.HTTPMessage, size: int) -> str | None:
    """Проверяет, что тело пришло целиком.

    При чтении кусками http.client не бросает IncompleteRead, если сервер закрыл
    соединение раньше времени. При chunked-ответе Content-Length по стандарту
    игнорируется, а обрыв http.client замечает сам.
    """
    if "chunked" in headers.get("Transfer-Encoding", "").lower():
        return None
    expected = headers.get("Content-Length", "")
    if expected.isdigit() and size < int(expected):
        return f"соединение оборвалось: получено {size} из {expected} байт"
    return None


def describe_error(exc: BaseException) -> str:
    """Превращает сетевое исключение в короткое сообщение в одну строку."""
    if isinstance(exc, urllib.error.HTTPError):
        # У ошибки бесконечного редиректа reason многострочный.
        reason = str(exc.reason).strip().split("\n", 1)[0]
        return f"HTTP {exc.code} {reason}"
    if isinstance(exc, urllib.error.URLError):
        # urllib заворачивает настоящую причину (DNS, тайм-аут, отказ) в URLError.
        if isinstance(exc.reason, BaseException):
            return describe_error(exc.reason)
        return f"ошибка соединения: {exc.reason}"
    if isinstance(exc, TimeoutError):
        return "тайм-аут"
    if isinstance(exc, socket.gaierror):
        return f"не удалось найти хост (DNS): {exc}"
    # verify_message и reason заполняет модуль ssl, у исключений из других мест их нет.
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"не прошла проверка SSL-сертификата: {getattr(exc, 'verify_message', None) or exc}"
    if isinstance(exc, ssl.SSLError):
        return f"ошибка SSL: {getattr(exc, 'reason', None) or exc}"
    if isinstance(exc, ConnectionRefusedError):
        return "сервер отклонил соединение"
    # RemoteDisconnected наследуется от ConnectionResetError, поэтому проверяем раньше.
    if isinstance(exc, http.client.RemoteDisconnected):
        return "сервер закрыл соединение, не отправив ответ"
    if isinstance(exc, ConnectionResetError):
        return "сервер сбросил соединение"
    if isinstance(exc, http.client.IncompleteRead):
        return "соединение оборвалось до конца ответа"
    return f"{type(exc).__name__}: {exc}"


def measure_all(url: str, count: int, timeout: float) -> Iterator[RequestResult]:
    """Выполняет запросы строго по очереди и отдаёт результат каждого сразу.

    Генератор нужен, чтобы печатать прогресс по ходу замера и не терять
    полученные результаты при Ctrl+C.
    """
    for number in range(1, count + 1):
        yield measure_request(url, timeout, number)


# --- Статистика --------------------------------------------------------------


def mbit_per_s(size: int, seconds: float) -> float | None:
    return size * 8 / BYTES_IN_MB / seconds if seconds > 0 else None


def summarize(results: Sequence[RequestResult]) -> Summary:
    """Считает статистику по успешным запросам.

    Скорость = суммарные байты / суммарное время, а не среднее скоростей
    отдельных запросов: иначе один быстрый запрос завышал бы итог.
    """
    ok = [r for r in results if r.ok]
    if not ok:
        return Summary(len(results), 0, 0, None, None, None, None)
    total_bytes = sum(r.size for r in ok)
    total_time = sum(r.elapsed for r in ok)
    total_ttfb = sum(r.ttfb for r in ok)
    return Summary(
        total=len(results),
        succeeded=len(ok),
        total_bytes=total_bytes,
        avg_time=total_time / len(ok),
        avg_ttfb=total_ttfb / len(ok),
        download_speed_mbit_s=mbit_per_s(total_bytes, total_time - total_ttfb),
        request_speed_mbit_s=mbit_per_s(total_bytes, total_time),
    )


def collect_warnings(summary: Summary) -> list[str]:
    """Предупреждения о том, что результату нельзя доверять."""
    warnings = []
    if summary.succeeded and summary.total_bytes / summary.succeeded < SMALL_RESPONSE_BYTES:
        avg_kb = summary.total_bytes / summary.succeeded / 1000
        warnings.append(
            f"ответ в среднем всего {avg_kb:.0f} КБ, на таком объёме скорость неточная."
            " Возможно, адрес ведёт на страницу, а не на сам файл. Лучше взять файл от 10 МБ."
        )
    return warnings


# --- Вывод -------------------------------------------------------------------


def format_speed(mbit_s: float | None) -> str:
    if mbit_s is None:
        return "н/д"
    return f"{mbit_s:.2f} Мбит/с ({mbit_s / 8:.2f} МБ/с)"


def format_result(result: RequestResult, total: int) -> str:
    width = len(str(total))
    prefix = f"[{result.number:>{width}}/{total}]"
    if not result.ok:
        return f"{prefix}  ОШИБКА: {result.error} (через {result.elapsed:.3f} с)"
    speed = mbit_per_s(result.size, result.elapsed - result.ttfb)
    speed_text = "н/д" if speed is None else f"{speed:.2f}"
    return (
        f"{prefix}  {result.elapsed:6.3f} с"
        f"  (первый байт {result.ttfb:.3f} с)"
        f"  {result.size / BYTES_IN_MB:8.2f} МБ"
        f"  {speed_text:>8} Мбит/с"
    )


def format_summary(summary: Summary) -> str:
    lines = [
        "-" * 64,
        f"Успешных запросов:        {summary.succeeded}/{summary.total}",
    ]
    if not summary.succeeded:
        lines.append("Ни один запрос не выполнился, скорость посчитать нельзя.")
        return "\n".join(lines)
    lines += [
        f"Скачано всего:            {summary.total_bytes / BYTES_IN_MB:.2f} МБ"
        f" ({summary.total_bytes} байт)",
        f"Среднее время запроса:    {summary.avg_time:.3f} с"
        f" (до первого байта {summary.avg_ttfb:.3f} с)",
        f"Скорость скачивания:      {format_speed(summary.download_speed_mbit_s)}",
        f"С учётом ожидания ответа: {format_speed(summary.request_speed_mbit_s)}",
    ]
    lines += [f"\nВнимание: {warning}" for warning in collect_warnings(summary)]
    return "\n".join(lines)


def format_json(url: str, results: Sequence[RequestResult], summary: Summary) -> str:
    def rounded(data: dict[str, object]) -> dict[str, object]:
        return {k: round(v, 6) if isinstance(v, float) else v for k, v in data.items()}

    payload = {
        "url": url,
        "requests": [rounded(asdict(r)) for r in results],
        "summary": rounded(asdict(summary)),
        "warnings": collect_warnings(summary),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# --- Командная строка --------------------------------------------------------


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается целое число, получено {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError("должно быть целым числом >= 1")
    return number


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается число, получено {value!r}") from None
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("должно быть конечным числом > 0")
    return number


class RussianHelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, "использование: " if prefix is None else prefix)


class RussianArgumentParser(argparse.ArgumentParser):
    """ArgumentParser, у которого сообщения об ошибках на русском.

    Заголовки справки задаются группами аргументов в parse_args, а тексты ошибок
    argparse формирует сам на английском, поэтому переводим их здесь.
    """

    ERROR_TRANSLATIONS = (
        (r"^argument (\S+): ", r"\1: "),
        (r"^the following arguments are required: ", "не указаны обязательные аргументы: "),
        (r"^unrecognized arguments: ", "неизвестные аргументы: "),
        (r"expected one argument$", "не указано значение"),
        (r"ignored explicit argument (.+)$", r"значение \1 не поддерживается"),
    )

    def error(self, message: str) -> NoReturn:
        for pattern, replacement in self.ERROR_TRANSLATIONS:
            message = re.sub(pattern, replacement, message)
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: ошибка: {message}\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = RussianArgumentParser(
        description="Замер скорости интернета: N последовательных скачиваний файла по URL.",
        formatter_class=RussianHelpFormatter,
        add_help=False,
    )
    arguments = parser.add_argument_group("аргументы")
    options = parser.add_argument_group("параметры")
    arguments.add_argument("url", help="адрес тяжёлого файла, например большой картинки")
    options.add_argument("-h", "--help", action="help", help="показать эту справку и выйти")
    options.add_argument(
        "-n",
        "--requests",
        type=positive_int,
        default=DEFAULT_REQUESTS,
        metavar="N",
        help=f"сколько запросов выполнить (по умолчанию {DEFAULT_REQUESTS})",
    )
    options.add_argument(
        "-t",
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT,
        metavar="СЕК",
        help=f"тайм-аут ожидания данных от сервера, с (по умолчанию {DEFAULT_TIMEOUT:g})",
    )
    options.add_argument(
        "--json",
        action="store_true",
        help="вывести результат в JSON вместо текста",
    )
    args = parser.parse_args(argv)
    try:
        args.url = normalize_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.json:
        print(f"Замер скорости: {args.url}")
        print(f"Запросов: {args.requests}, тайм-аут: {args.timeout:g} с\n", flush=True)

    results: list[RequestResult] = []
    interrupted = False
    try:
        for result in measure_all(args.url, args.requests, args.timeout):
            results.append(result)
            if not args.json:
                print(format_result(result, args.requests), flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print("\nПрервано пользователем, итог по выполненным запросам:", file=sys.stderr)

    summary = summarize(results)
    if args.json:
        print(format_json(args.url, results, summary))
    else:
        print(format_summary(summary))
    if interrupted:
        return EXIT_INTERRUPTED
    return EXIT_OK if summary.succeeded else EXIT_ALL_FAILED


def configure_output() -> None:
    """При выводе в файл или пайп пишем UTF-8.

    Иначе Python берёт кодировку системы, и на Windows с cp1252
    `python speed_meter.py URL > result.txt` падает на первой кириллической строке.
    """
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty() and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def cli() -> int:
    configure_output()
    try:
        exit_code = main()
        sys.stdout.flush()
    except OSError as exc:
        # Читатель вывода закрылся раньше времени, например `| head -3`.
        # Unix сообщает об этом как EPIPE, Windows как EINVAL. Сетевые OSError
        # сюда не доходят: их обрабатывает measure_request.
        if exc.errno not in (errno.EPIPE, errno.EINVAL):
            raise
        # Без этого Python при выходе ещё раз попробует сбросить буфер и упадёт.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return EXIT_BROKEN_PIPE
    return exit_code


if __name__ == "__main__":
    sys.exit(cli())
