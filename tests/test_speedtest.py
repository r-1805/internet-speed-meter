import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import speedtest

FILE_SIZE = 300_000  # больше CHUNK_SIZE, чтобы проверить чтение в несколько кусков
SCRIPT = Path(__file__).resolve().parent.parent / "speedtest.py"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # нужен для chunked-ответов

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
    return speedtest.RequestResult(
        number=number, elapsed=elapsed, ttfb=ttfb, size=size, error=error
    )


# --- measure_request ---------------------------------------------------------


def test_downloads_whole_body(base_url):
    result = speedtest.measure_request(f"{base_url}/file", timeout=5)
    assert result.ok
    assert result.size == FILE_SIZE
    assert 0 < result.ttfb <= result.elapsed


def test_reads_until_connection_close_without_content_length(base_url):
    result = speedtest.measure_request(f"{base_url}/no-length", timeout=5)
    assert result.ok
    assert result.size == FILE_SIZE


def test_reads_chunked_body(base_url):
    result = speedtest.measure_request(f"{base_url}/chunked", timeout=5)
    assert result.ok
    assert result.size == FILE_SIZE


def test_follows_redirect(base_url):
    result = speedtest.measure_request(f"{base_url}/redirect", timeout=5)
    assert result.ok
    assert result.size == FILE_SIZE


def test_http_error_is_reported(base_url):
    result = speedtest.measure_request(f"{base_url}/missing", timeout=5)
    assert not result.ok
    assert result.error.startswith("HTTP 404")


def test_redirect_loop_error_is_single_line(base_url):
    result = speedtest.measure_request(f"{base_url}/redirect-loop", timeout=5)
    assert result.error.startswith("HTTP 302")
    assert "\n" not in result.error


def test_timeout_waiting_for_headers(base_url):
    result = speedtest.measure_request(f"{base_url}/slow", timeout=0.2)
    assert result.error == "тайм-аут"


def test_timeout_in_the_middle_of_body(base_url):
    result = speedtest.measure_request(f"{base_url}/stall", timeout=0.2)
    assert result.error == "тайм-аут"
    assert result.ttfb is not None


def test_truncated_body_is_an_error(base_url):
    result = speedtest.measure_request(f"{base_url}/broken", timeout=5)
    assert not result.ok
    assert "получено 1000 из 300000 байт" in result.error


def test_truncated_chunked_body_is_an_error(base_url):
    result = speedtest.measure_request(f"{base_url}/chunked-broken", timeout=5)
    assert not result.ok


def test_connection_refused_is_reported():
    # Порт 1 на localhost почти наверняка закрыт.
    result = speedtest.measure_request("http://127.0.0.1:1/", timeout=2)
    assert not result.ok


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
    assert speedtest.normalize_url(url) == expected


@pytest.mark.parametrize("url", ["ftp://example.com/f", "example.com/f", "http://", "http://[::1"])
def test_normalize_url_rejects_invalid(url):
    with pytest.raises(ValueError):
        speedtest.normalize_url(url)


def test_non_ascii_url_is_downloaded(base_url):
    url = speedtest.normalize_url(f"{base_url}/Тест file")
    result = speedtest.measure_request(url, timeout=5)
    assert result.ok
    assert result.size == FILE_SIZE


# --- run ---------------------------------------------------------------------


def test_run_makes_requests_sequentially_and_reports_progress(base_url):
    seen = []
    results = speedtest.run(f"{base_url}/file", count=3, timeout=5, on_result=seen.append)
    assert [r.number for r in results] == [1, 2, 3]
    assert seen == results


# --- summarize ---------------------------------------------------------------


def test_summary_uses_total_bytes_over_total_time():
    results = [
        make_result(1, elapsed=1.0, size=1_000_000),
        make_result(2, elapsed=3.0, size=1_000_000),
    ]
    summary = speedtest.summarize(results)
    assert summary.total_bytes == 2_000_000
    assert summary.avg_time == pytest.approx(2.0)
    # 2 МБ за 4 с = 0.5 МБ/с = 4 Мбит/с (а не среднее из 8 и 2.67 Мбит/с).
    assert summary.speed_mb_s == pytest.approx(0.5)
    assert summary.speed_mbit_s == pytest.approx(4.0)


def test_summary_ignores_failed_requests():
    results = [
        make_result(1, elapsed=2.0, size=1_000_000),
        make_result(2, elapsed=30.0, size=0, ttfb=None, error="тайм-аут"),
    ]
    summary = speedtest.summarize(results)
    assert (summary.total, summary.succeeded) == (2, 1)
    assert summary.avg_time == pytest.approx(2.0)
    assert summary.speed_mbit_s == pytest.approx(4.0)


def test_summary_when_everything_failed():
    summary = speedtest.summarize([make_result(1, elapsed=1.0, size=0, error="HTTP 404")])
    assert summary.succeeded == 0
    assert summary.speed_mbit_s == 0.0
    assert summary.avg_time == 0.0


def test_summary_of_empty_list():
    summary = speedtest.summarize([])
    assert (summary.total, summary.succeeded, summary.speed_mbit_s) == (0, 0, 0.0)


# --- CLI ---------------------------------------------------------------------


def test_main_prints_summary(base_url, capsys):
    code = speedtest.main([f"{base_url}/file", "-n", "2"])
    out = capsys.readouterr().out
    assert code == speedtest.EXIT_OK
    assert "[1/2]" in out and "[2/2]" in out
    assert "Успешных запросов:     2/2" in out
    assert "Мбит/с" in out


def test_default_is_ten_requests(base_url, capsys):
    assert speedtest.main([f"{base_url}/file"]) == speedtest.EXIT_OK
    out = capsys.readouterr().out
    assert "[10/10]" in out
    assert "10/10" in out


def test_main_json_output(base_url, capsys):
    code = speedtest.main([f"{base_url}/file", "-n", "2", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == speedtest.EXIT_OK
    assert len(data["requests"]) == 2
    assert data["summary"]["total_bytes"] == 2 * FILE_SIZE


def test_main_returns_error_code_when_all_requests_fail(base_url, capsys):
    assert speedtest.main([f"{base_url}/missing", "-n", "2"]) == speedtest.EXIT_ALL_FAILED


def test_main_succeeds_when_some_requests_fail(monkeypatch, capsys):
    outcomes = iter([make_result(1, 1.0, 1000), make_result(2, 1.0, 0, error="тайм-аут")])
    monkeypatch.setattr(speedtest, "measure_request", lambda *a, **kw: next(outcomes))
    assert speedtest.main(["http://example.com/f", "-n", "2"]) == speedtest.EXIT_OK
    assert "Успешных запросов:     1/2" in capsys.readouterr().out


def test_ctrl_c_prints_summary_for_completed_requests(monkeypatch, capsys):
    calls = []

    def fake_measure(url, timeout, number=1):
        calls.append(number)
        if number == 3:
            raise KeyboardInterrupt
        return make_result(number, elapsed=1.0, size=1_000_000)

    monkeypatch.setattr(speedtest, "measure_request", fake_measure)
    code = speedtest.main(["http://example.com/f"])
    captured = capsys.readouterr()
    assert code == speedtest.EXIT_INTERRUPTED
    assert calls == [1, 2, 3]
    assert "Успешных запросов:     2/2" in captured.out
    assert "Прервано" in captured.err


@pytest.mark.parametrize(
    "argv",
    [
        ["ftp://example.com/file"],
        ["example.com/file"],
        ["http://[::1"],
        ["https://example.com", "-n", "0"],
        ["https://example.com", "-n", "abc"],
        ["https://example.com", "-t", "-1"],
        ["https://example.com", "-t", "nan"],
        ["https://example.com", "-t", "inf"],
    ],
)
def test_invalid_arguments(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        speedtest.main(argv)
    assert exc.value.code == 2
    assert "invalid" not in capsys.readouterr().err  # сообщения argparse заменены на русские


def test_redirected_output_does_not_crash_on_legacy_encoding(base_url):
    # Имитируем `python speedtest.py URL > result.txt` на Windows с кодировкой cp1252.
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), f"{base_url}/file", "-n", "1"],
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "Средняя скорость" in proc.stdout.decode("utf-8")
