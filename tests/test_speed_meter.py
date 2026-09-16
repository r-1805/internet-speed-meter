import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import speed_meter

FILE_SIZE = 300_000  # больше CHUNK_SIZE, чтобы проверить чтение в несколько кусков
SCRIPT = Path(__file__).resolve().parent.parent / "speed_meter.py"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # нужен для chunked-ответов
    # Для проверки последовательности: сколько запросов /track обрабатывается одновременно.
    lock = threading.Lock()
    active = 0
    max_active = 0

    def do_GET(self):
        routes = {
            "/file": self.send_file,
            "/no-length": self.send_without_length,
            "/broken": self.send_truncated,
            "/chunked": self.send_chunked,
            "/chunked-broken": self.send_chunked_truncated,
            "/stall": self.send_and_stall,
            "/slow": self.send_slow_headers,
            "/redirect": self.send_redirect,
            "/redirect-loop": self.send_redirect_loop,
            "/close": self.close_without_response,
            "/chunked-with-length": self.send_chunked_with_length,
            "/track": self.send_tracked,
            "/%D0%A2%D0%B5%D1%81%D1%82%20file": self.send_file,  # "/Тест file"
        }
        routes.get(self.path, lambda: self.send_error(404))()

    def send_file(self):
        self.send_response(200)
        self.send_header("Content-Length", str(FILE_SIZE))
        self.end_headers()
        self.wfile.write(b"x" * FILE_SIZE)

    def send_without_length(self):
        # Без Content-Length клиент должен читать до закрытия соединения.
        self.send_response(200)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"y" * FILE_SIZE)
        self.close_connection = True

    def send_truncated(self):
        # Обещаем больше, чем отдаём, и закрываем соединение.
        self.send_response(200)
        self.send_header("Content-Length", str(FILE_SIZE))
        self.end_headers()
        self.wfile.write(b"z" * 1000)
        self.close_connection = True

    def send_chunked(self, chunks=10, last=True):
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunk = b"c" * (FILE_SIZE // 10)
        for _ in range(chunks):
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        if last:
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.close_connection = True

    def send_chunked_truncated(self):
        self.send_chunked(chunks=3, last=False)

    def send_and_stall(self):
        # Заголовки и часть тела пришли, дальше сервер молчит.
        self.send_response(200)
        self.send_header("Content-Length", str(FILE_SIZE))
        self.end_headers()
        self.wfile.write(b"s" * 1000)
        self.wfile.flush()
        time.sleep(1)
        self.close_connection = True

    def send_slow_headers(self):
        time.sleep(1)
        self.send_file()

    def send_redirect(self):
        self.send_response(302)
        self.send_header("Location", "/file")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_redirect_loop(self):
        self.send_response(302)
        self.send_header("Location", "/redirect-loop")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_chunked_with_length(self):
        # По стандарту при chunked Content-Length игнорируется, даже если он неверный.
        self.send_response(200)
        self.send_header("Content-Length", "999999")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write(b"5\r\nhello\r\n0\r\n\r\n")

    def send_tracked(self):
        cls = type(self)
        with cls.lock:
            cls.active += 1
            cls.max_active = max(cls.max_active, cls.active)
        time.sleep(0.05)
        self.send_file()
        with cls.lock:
            cls.active -= 1

    def close_without_response(self):
        self.close_connection = True

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="module")
def base_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def make_result(number, elapsed, size, ttfb=0.1, error=None):
    return speed_meter.RequestResult(
        number=number, elapsed=elapsed, ttfb=ttfb, size=size, error=error
    )


# --- measure_request ---------------------------------------------------------


def test_downloads_whole_body(base_url):
    result = speed_meter.measure_request(f"{base_url}/file", timeout=5, number=1)
    assert result.ok
    assert result.size == FILE_SIZE
    assert 0 < result.ttfb <= result.elapsed


def test_reads_until_connection_close_without_content_length(base_url):
    result = speed_meter.measure_request(f"{base_url}/no-length", timeout=5, number=1)
    assert result.ok
    assert result.size == FILE_SIZE


def test_reads_chunked_body(base_url):
    result = speed_meter.measure_request(f"{base_url}/chunked", timeout=5, number=1)
    assert result.ok
    assert result.size == FILE_SIZE


def test_follows_redirect(base_url):
    result = speed_meter.measure_request(f"{base_url}/redirect", timeout=5, number=1)
    assert result.ok
    assert result.size == FILE_SIZE


def test_http_error_is_reported(base_url):
    result = speed_meter.measure_request(f"{base_url}/missing", timeout=5, number=1)
    assert not result.ok
    assert result.error.startswith("HTTP 404")


def test_redirect_loop_error_is_single_line(base_url):
    result = speed_meter.measure_request(f"{base_url}/redirect-loop", timeout=5, number=1)
    assert result.error.startswith("HTTP 302")
    assert "\n" not in result.error


def test_timeout_waiting_for_headers(base_url):
    result = speed_meter.measure_request(f"{base_url}/slow", timeout=0.2, number=1)
    assert result.error == "тайм-аут"


def test_timeout_in_the_middle_of_body(base_url):
    result = speed_meter.measure_request(f"{base_url}/stall", timeout=0.2, number=1)
    assert result.error == "тайм-аут"
    assert result.ttfb is not None


def test_truncated_body_is_an_error(base_url):
    result = speed_meter.measure_request(f"{base_url}/broken", timeout=5, number=1)
    assert not result.ok
    assert "получено 1000 из 300000 байт" in result.error


def test_truncated_chunked_body_is_an_error(base_url):
    result = speed_meter.measure_request(f"{base_url}/chunked-broken", timeout=5, number=1)
    assert result.error == "соединение оборвалось до конца ответа"


def test_chunked_response_ignores_content_length(base_url):
    result = speed_meter.measure_request(f"{base_url}/chunked-with-length", timeout=5, number=1)
    assert result.ok, result.error
    assert result.size == 5


def test_server_closed_connection_without_response(base_url):
    result = speed_meter.measure_request(f"{base_url}/close", timeout=5, number=1)
    assert result.error == "сервер закрыл соединение, не отправив ответ"


def test_connection_refused_is_reported():
    # Берём свободный порт и закрываем его, чтобы на нём точно никто не слушал.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Windows повторяет попытку соединения около 2 с, поэтому тайм-аут с запасом.
    result = speed_meter.measure_request(f"http://127.0.0.1:{port}/", timeout=10, number=1)
    assert result.error == "сервер отклонил соединение"


def ssl_verify_error(message):
    exc = ssl.SSLCertVerificationError(1, f"certificate verify failed: {message}")
    exc.verify_message = message  # в реальном исключении атрибут заполняет модуль ssl
    return exc


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")),
            "не удалось найти хост",
        ),
        (urllib.error.URLError(TimeoutError("timed out")), "тайм-аут"),
        (urllib.error.URLError("no host given"), "ошибка соединения: no host given"),
        (ConnectionResetError(10054, "reset"), "сервер сбросил соединение"),
        (ssl_verify_error("certificate has expired"), "не прошла проверка SSL-сертификата"),
        (urllib.error.URLError(ssl.SSLError(1, "wrong version")), "ошибка SSL"),
        (BrokenPipeError(32, "broken pipe"), "BrokenPipeError: [Errno 32] broken pipe"),
    ],
)
def test_describe_error(exc, expected):
    message = speed_meter.describe_error(exc)
    assert message.startswith(expected)
    assert "\n" not in message


# --- normalize_url -----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a.jpg", "https://example.com/a.jpg"),
        ("  https://example.com/a.jpg  ", "https://example.com/a.jpg"),
        ("https://example.com/a.jpg#frag", "https://example.com/a.jpg"),
        (
            "https://ru.wikipedia.org/wiki/Тест",
            "https://ru.wikipedia.org/wiki/%D0%A2%D0%B5%D1%81%D1%82",
        ),
        ("https://example.com/a b.jpg", "https://example.com/a%20b.jpg"),
        ("https://example.com/a%20b.jpg?x=1&y=2", "https://example.com/a%20b.jpg?x=1&y=2"),
        (
            "http://пример.рф:8080/файл",
            "http://xn--e1afmkfd.xn--p1ai:8080/%D1%84%D0%B0%D0%B9%D0%BB",
        ),
        ("HTTPS://example.com/", "https://example.com/"),
    ],
)
def test_normalize_url(url, expected):
    assert speed_meter.normalize_url(url) == expected


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://example.com/f", "URL должен начинаться с http:// или https://"),
        ("example.com/f", "URL должен начинаться с http:// или https://"),
        ("http://", "в URL не указан хост"),
        ("http://[::1", "некорректный URL"),
        ("http://example.com:99999/", "некорректный порт в URL"),
        ("http://" + "а" * 70 + ".рф/", "некорректное доменное имя"),
    ],
)
def test_normalize_url_rejects_invalid(url, message):
    with pytest.raises(ValueError, match=message):
        speed_meter.normalize_url(url)


def test_non_ascii_url_is_downloaded(base_url):
    url = speed_meter.normalize_url(f"{base_url}/Тест file")
    result = speed_meter.measure_request(url, timeout=5, number=1)
    assert result.ok
    assert result.size == FILE_SIZE


# --- measure_all -------------------------------------------------------------


def test_measure_all_runs_requests_one_after_another(base_url):
    Handler.max_active = 0
    results = list(speed_meter.measure_all(f"{base_url}/track", count=5, timeout=5))
    assert [r.number for r in results] == [1, 2, 3, 4, 5]
    assert all(r.ok for r in results)
    assert Handler.max_active == 1  # ни разу не было двух запросов одновременно


def test_measure_all_yields_each_result_before_next_request(monkeypatch):
    calls = []

    def fake_measure(url, timeout, number):
        calls.append(number)
        return make_result(number, elapsed=1.0, size=1)

    monkeypatch.setattr(speed_meter, "measure_request", fake_measure)
    measurements = speed_meter.measure_all("http://example.com/", count=3, timeout=1)
    assert next(measurements).number == 1
    assert calls == [1]  # следующий запрос не начат, пока не забрали результат


# --- summarize ---------------------------------------------------------------


def test_summary_uses_total_bytes_over_total_time():
    results = [
        make_result(1, elapsed=1.0, size=1_000_000, ttfb=0.0),
        make_result(2, elapsed=3.0, size=1_000_000, ttfb=0.0),
    ]
    summary = speed_meter.summarize(results)
    assert summary.total_bytes == 2_000_000
    assert summary.avg_time == pytest.approx(2.0)
    # 2 МБ за 4 с = 4 Мбит/с, а не среднее из 8 и 2.67 Мбит/с.
    assert summary.download_speed_mbit_s == pytest.approx(4.0)


def test_download_speed_excludes_time_to_first_byte():
    # 1 МБ: 0.5 с ждали ответ, 0.5 с качали тело.
    summary = speed_meter.summarize([make_result(1, elapsed=1.0, size=1_000_000, ttfb=0.5)])
    assert summary.avg_ttfb == pytest.approx(0.5)
    assert summary.download_speed_mbit_s == pytest.approx(16.0)
    assert summary.request_speed_mbit_s == pytest.approx(8.0)


def test_summary_ignores_failed_requests():
    results = [
        make_result(1, elapsed=2.0, size=1_000_000, ttfb=1.0),
        make_result(2, elapsed=30.0, size=0, ttfb=None, error="тайм-аут"),
    ]
    summary = speed_meter.summarize(results)
    assert (summary.total, summary.succeeded) == (2, 1)
    assert summary.avg_time == pytest.approx(2.0)
    assert summary.download_speed_mbit_s == pytest.approx(8.0)


@pytest.mark.parametrize(
    "results", [[], [make_result(1, elapsed=1.0, size=0, ttfb=None, error="HTTP 404")]]
)
def test_summary_without_successful_requests_has_no_metrics(results):
    summary = speed_meter.summarize(results)
    assert summary.succeeded == 0
    assert summary.avg_time is None
    assert summary.download_speed_mbit_s is None
    assert summary.request_speed_mbit_s is None


def test_warns_about_small_responses():
    small = speed_meter.summarize([make_result(1, elapsed=0.2, size=1_118)])
    (warning,) = speed_meter.collect_warnings(small)
    assert "1 КБ" in warning
    assert "Внимание" in speed_meter.format_summary(small)

    big = speed_meter.summarize([make_result(1, elapsed=1.0, size=15_000_000)])
    assert speed_meter.collect_warnings(big) == []
    assert "Внимание" not in speed_meter.format_summary(big)


def test_zero_download_time_does_not_divide_by_zero():
    result = make_result(1, elapsed=0.1, size=10, ttfb=0.1)
    summary = speed_meter.summarize([result])
    assert summary.download_speed_mbit_s is None
    assert "н/д" in speed_meter.format_result(result, total=1)
    assert "Скорость скачивания:      н/д" in speed_meter.format_summary(summary)


# --- CLI ---------------------------------------------------------------------


def test_main_prints_summary(base_url, capsys):
    code = speed_meter.main([f"{base_url}/file", "-n", "2"])
    out = capsys.readouterr().out
    assert code == speed_meter.EXIT_OK
    assert "[1/2]" in out and "[2/2]" in out
    assert "Успешных запросов:        2/2" in out
    assert "Мбит/с" in out


def test_default_is_ten_requests(base_url, capsys):
    assert speed_meter.main([f"{base_url}/file"]) == speed_meter.EXIT_OK
    out = capsys.readouterr().out
    assert "[10/10]" in out


def test_main_json_output(base_url, capsys):
    code = speed_meter.main([f"{base_url}/file", "-n", "2", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == speed_meter.EXIT_OK
    assert len(data["requests"]) == 2
    assert data["summary"]["total_bytes"] == 2 * FILE_SIZE
    assert len(data["warnings"]) == 1  # тестовый файл 300 КБ меньше порога
    floats = [v for r in data["requests"] for v in r.values() if isinstance(v, float)]
    assert floats and all(v == round(v, 6) for v in floats)


def test_text_output_when_nothing_succeeded(base_url, capsys):
    code = speed_meter.main([f"{base_url}/missing", "-n", "2"])
    out = capsys.readouterr().out
    assert code == speed_meter.EXIT_ALL_FAILED
    assert "ОШИБКА: HTTP 404" in out
    assert "Ни один запрос не выполнился" in out
    assert "Скорость скачивания" not in out


def test_json_has_nulls_when_nothing_succeeded(base_url, capsys):
    code = speed_meter.main([f"{base_url}/missing", "-n", "2", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == speed_meter.EXIT_ALL_FAILED
    assert data["requests"][0]["error"].startswith("HTTP 404")
    assert data["summary"]["download_speed_mbit_s"] is None
    assert data["summary"]["avg_time"] is None


def test_main_succeeds_when_some_requests_fail(monkeypatch, capsys):
    outcomes = iter([make_result(1, 1.0, 1000), make_result(2, 1.0, 0, error="тайм-аут")])
    monkeypatch.setattr(speed_meter, "measure_request", lambda *a, **kw: next(outcomes))
    assert speed_meter.main(["http://example.com/f", "-n", "2"]) == speed_meter.EXIT_OK
    assert "Успешных запросов:        1/2" in capsys.readouterr().out


def test_ctrl_c_prints_summary_for_completed_requests(monkeypatch, capsys):
    calls = []

    def fake_measure(url, timeout, number):
        calls.append(number)
        if number == 3:
            raise KeyboardInterrupt
        return make_result(number, elapsed=1.0, size=1_000_000)

    monkeypatch.setattr(speed_meter, "measure_request", fake_measure)
    code = speed_meter.main(["http://example.com/f"])
    captured = capsys.readouterr()
    assert code == speed_meter.EXIT_INTERRUPTED
    assert calls == [1, 2, 3]
    assert "Успешных запросов:        2/2" in captured.out
    assert "Прервано" in captured.err


# Слова из встроенных английских сообщений argparse, которых не должно быть в выводе.
ARGPARSE_ENGLISH = ("usage", "error", "argument", "invalid", "required", "expected", "options")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "не указаны обязательные аргументы: url"),
        (["https://example.com", "--foo"], "неизвестные аргументы: --foo"),
        (["https://example.com", "-n"], "-n/--requests: не указано значение"),
        (["https://example.com", "--json=1"], "--json: значение '1' не поддерживается"),
        (["ftp://example.com/file"], "URL должен начинаться с http:// или https://"),
        (["example.com/file"], "URL должен начинаться с http:// или https://"),
        (["http://[::1"], "некорректный URL"),
        (["https://example.com", "-n", "0"], "-n/--requests: должно быть целым числом >= 1"),
        (["https://example.com", "-n", "abc"], "-n/--requests: ожидается целое число"),
        (["https://example.com", "-t", "-1"], "-t/--timeout: должно быть конечным числом > 0"),
        (["https://example.com", "-t", "abc"], "-t/--timeout: ожидается число"),
        (["https://example.com", "-t", "nan"], "-t/--timeout: должно быть конечным числом > 0"),
        (["https://example.com", "-t", "inf"], "-t/--timeout: должно быть конечным числом > 0"),
    ],
)
def test_invalid_arguments(argv, message, capsys):
    with pytest.raises(SystemExit) as exc:
        speed_meter.main(argv)
    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert "использование: " in err
    assert f"ошибка: {message}" in err
    assert not any(word in err.lower() for word in ARGPARSE_ENGLISH), err


def test_help_is_in_russian(capsys):
    with pytest.raises(SystemExit) as exc:
        speed_meter.main(["--help"])
    out = capsys.readouterr().out
    assert exc.value.code == 0
    for text in ("использование:", "аргументы:", "параметры:", "показать эту справку и выйти"):
        assert text in out
    assert not any(word in out.lower() for word in (*ARGPARSE_ENGLISH, "show this help"))


def test_valid_arguments_are_parsed():
    args = speed_meter.parse_args(["https://example.com/f", "-n", "3", "-t", "2.5", "--json"])
    assert (args.url, args.requests, args.timeout, args.json) == (
        "https://example.com/f",
        3,
        2.5,
        True,
    )


def test_redirected_output_does_not_crash_on_legacy_encoding(base_url):
    # Имитируем `python speed_meter.py URL > result.txt` на Windows с кодировкой cp1252.
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), f"{base_url}/file", "-n", "1"],
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "Скорость скачивания" in proc.stdout.decode("utf-8")


def test_closed_output_pipe_exits_quietly(base_url):
    # Имитируем `python speed_meter.py URL | head -1`: читатель закрывает пайп,
    # а скрипт ещё пишет. /slow отвечает через 1 с, так что запись точно будет после закрытия.
    with subprocess.Popen(
        [sys.executable, str(SCRIPT), f"{base_url}/slow", "-n", "2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        proc.stdout.readline()
        proc.stdout.close()
        stderr = proc.stderr.read().decode("utf-8", "replace")
        assert proc.wait(timeout=30) == speed_meter.EXIT_BROKEN_PIPE
    assert "Traceback" not in stderr


def all_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from all_subclasses(sub)


def test_describe_error_never_raises():
    # Ошибка внутри обработчика ошибок уронила бы весь замер, поэтому перебираем
    # все подклассы исключений, которые ловит measure_request.
    checked = 0
    for cls in {*all_subclasses(OSError), *all_subclasses(http.client.HTTPException)}:
        try:
            exc = cls()
        except TypeError:
            continue  # класс требует обязательных аргументов
        message = speed_meter.describe_error(exc)
        assert isinstance(message, str) and message
        assert "\n" not in message
        checked += 1
    assert checked > 20
