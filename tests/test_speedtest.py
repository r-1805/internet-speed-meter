import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import speedtest

FILE_SIZE = 300_000  # больше CHUNK_SIZE, чтобы проверить чтение в несколько кусков


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/file":
            body = b"x" * FILE_SIZE
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/no-length":
            # Без Content-Length: клиент должен читать до закрытия соединения.
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"y" * FILE_SIZE)
        elif self.path == "/broken":
            # Обещаем больше, чем отдаём, и закрываем соединение.
            self.send_response(200)
            self.send_header("Content-Length", str(FILE_SIZE))
            self.end_headers()
            self.wfile.write(b"z" * 1000)
        elif self.path == "/slow":
            time.sleep(1)
            self.send_response(200)
            self.end_headers()
        else:
            self.send_error(404)

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


def test_http_error_is_reported(base_url):
    result = speedtest.measure_request(f"{base_url}/missing", timeout=5)
    assert not result.ok
    assert result.error.startswith("HTTP 404")


def test_timeout_is_reported(base_url):
    result = speedtest.measure_request(f"{base_url}/slow", timeout=0.2)
    assert result.error == "тайм-аут"


def test_truncated_body_is_an_error(base_url):
    result = speedtest.measure_request(f"{base_url}/broken", timeout=5)
    assert not result.ok


def test_connection_refused_is_reported():
    # Порт 1 на localhost почти наверняка закрыт.
    result = speedtest.measure_request("http://127.0.0.1:1/", timeout=2)
    assert not result.ok


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


# --- CLI ---------------------------------------------------------------------


def test_main_prints_summary(base_url, capsys):
    code = speedtest.main([f"{base_url}/file", "-n", "2"])
    out = capsys.readouterr().out
    assert code == speedtest.EXIT_OK
    assert "[1/2]" in out and "[2/2]" in out
    assert "Успешных запросов:     2/2" in out
    assert "Мбит/с" in out


def test_main_json_output(base_url, capsys):
    code = speedtest.main([f"{base_url}/file", "-n", "2", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == speedtest.EXIT_OK
    assert len(data["requests"]) == 2
    assert data["summary"]["total_bytes"] == 2 * FILE_SIZE


def test_main_returns_error_code_when_all_requests_fail(base_url, capsys):
    assert speedtest.main([f"{base_url}/missing", "-n", "2"]) == speedtest.EXIT_ALL_FAILED


@pytest.mark.parametrize(
    "argv",
    [
        ["ftp://example.com/file"],
        ["example.com/file"],
        ["https://example.com", "-n", "0"],
        ["https://example.com", "-t", "-1"],
    ],
)
def test_invalid_arguments(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        speedtest.main(argv)
    assert exc.value.code == 2
