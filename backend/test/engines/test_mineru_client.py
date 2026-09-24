"""MinerU 客户端协议测试（tasklist 6.1 / 6.5）。

用 httpx.MockTransport 假造 MinerU，不联网、不消耗额度。覆盖：文本文件直读不调 API、
提交/上传/轮询/解压全链路、失败与超时、以及**密钥不得出现在异常信息里**。
"""

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from app.engines.mineru import MinerUClient, MinerUError

API_KEY = "sk-mineru-super-secret"


def make_zip(markdown: str | None = "解析正文", entry: str = "full.md") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if markdown is not None:
            archive.writestr(entry, markdown)
        archive.writestr("layout.json", "{}")
    return buffer.getvalue()


class Call:
    def __init__(self, request: httpx.Request) -> None:
        self.method = request.method
        self.url = str(request.url)
        self.body = request.content or b""
        self.auth = request.headers.get("Authorization")

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


class FakeMinerU:
    """最小可用的 MinerU 假服务（按提交的文件数返回地址与结果）。"""

    def __init__(
        self,
        *,
        poll_states: tuple[str, ...] = ("running", "done"),
        zip_bytes: bytes | None = None,
        zip_by_data_id: dict[str, bytes] | None = None,
        business_code: int = 0,
        submit_status: int = 200,
        upload_status: int = 200,
    ) -> None:
        self.poll_states = poll_states
        self.zip_bytes = zip_bytes if zip_bytes is not None else make_zip()
        self.zip_by_data_id = zip_by_data_id or {}
        self.business_code = business_code
        self.submit_status = submit_status
        self.upload_status = upload_status
        self.calls: list[Call] = []
        self.poll_count = 0
        self.submitted_names: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # --- 断言辅助 ---------------------------------------------------------

    def calls_to(self, fragment: str) -> list[Call]:
        return [call for call in self.calls if fragment in call.url]

    # --- 路由 -------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        call = Call(request)
        self.calls.append(call)
        url = call.url

        if url.endswith("/file-urls/batch"):
            if self.submit_status >= 400:
                return httpx.Response(self.submit_status, json={"msg": "boom"})
            self.submitted_names = [item["name"] for item in call.json().get("files", [])]
            return httpx.Response(
                200,
                json={
                    "code": self.business_code,
                    "msg": "ok" if self.business_code == 0 else "quota exceeded",
                    "data": {
                        "batch_id": "batch-1",
                        "file_urls": [
                            f"https://upload.example.com/presigned-{index}"
                            for index in range(len(self.submitted_names))
                        ],
                    },
                },
            )

        if url.startswith("https://upload.example.com/"):
            return httpx.Response(self.upload_status)

        if "extract-results" in url:
            state = self.poll_states[min(self.poll_count, len(self.poll_states) - 1)]
            self.poll_count += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-1",
                        "extract_result": [
                            {
                                "file_name": name,
                                "data_id": str(position),
                                "state": state,
                                "full_zip_url": f"https://result.example.com/out.zip?d={position}",
                                "err_msg": "解析器内部错误" if state == "failed" else "",
                            }
                            for position, name in enumerate(self.submitted_names)
                        ],
                    },
                },
            )

        if url.startswith("https://result.example.com/"):
            data_id = request.url.params.get("d", "")
            return httpx.Response(200, content=self.zip_by_data_id.get(data_id, self.zip_bytes))

        raise AssertionError(f"未预期的请求：{request.method} {url}")


def make_client(fake: FakeMinerU, **overrides) -> MinerUClient:
    params = {
        "base_url": "https://mineru.example.com/api/v4",
        "api_key": API_KEY,
        "poll_interval": 0,
        "timeout": 1,
        "transport": fake.transport(),
    }
    params.update(overrides)
    return MinerUClient(**params)


def write(tmp_path: Path, name: str, content: bytes | str) -> Path:
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


# --- 文本文件不调用 API ---------------------------------------------------


@pytest.mark.parametrize("name", ["制度.md", "制度.markdown", "制度.txt"])
async def test_text_files_are_read_locally_without_api(tmp_path, name) -> None:
    """md/markdown/txt 直读，一个 HTTP 请求都不该发。"""

    fake = FakeMinerU()
    path = write(tmp_path, name, "本地制度正文")

    result = await make_client(fake).parse_batch([path])

    assert result[str(path)] == "本地制度正文"
    assert fake.calls == [], "文本文件不得调用 MinerU"


async def test_text_file_with_bom_is_stripped(tmp_path) -> None:
    path = write(tmp_path, "制度.txt", "正文".encode("utf-8-sig"))
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")

    result = await make_client(FakeMinerU()).parse_batch([path])

    assert result[str(path)] == "正文"


async def test_non_utf8_text_file_raises(tmp_path) -> None:
    path = write(tmp_path, "旧制度.txt", "报销".encode("gbk"))

    with pytest.raises(MinerUError, match="UTF-8"):
        await make_client(FakeMinerU()).parse_batch([path])


async def test_mixed_batch_only_sends_pdf_to_api(tmp_path) -> None:
    fake = FakeMinerU()
    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")
    text = write(tmp_path, "说明.md", "说明正文")

    result = await make_client(fake).parse_batch([pdf, text])

    assert result[str(text)] == "说明正文"
    assert result[str(pdf)] == "解析正文"
    assert len(fake.calls_to("/file-urls/batch")) == 1


# --- 全链路 ---------------------------------------------------------------


async def test_pdf_happy_path_submit_upload_poll_extract(tmp_path) -> None:
    fake = FakeMinerU()
    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")

    result = await make_client(fake).parse_batch([pdf])

    assert result[str(pdf)] == "解析正文"

    assert [call.method for call in fake.calls] == ["POST", "PUT", "GET", "GET", "GET"], (
        "应依次提交、上传、轮询到终态、下载"
    )
    assert fake.poll_count == 2, "第一次 running，第二次 done"

    body = fake.calls[0].json()
    assert body["files"] == [{"name": "制度.pdf", "data_id": "0"}]
    assert body["model_version"] and body["language"]

    assert fake.calls[1].body == b"%PDF-1.4 fake", "PUT 的必须是原件二进制"


async def test_auth_header_only_on_mineru_endpoints(tmp_path) -> None:
    """提交与轮询带 Bearer；上传与下载是 presigned，不应带鉴权头。"""

    fake = FakeMinerU()
    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")

    await make_client(fake).parse_batch([pdf])

    assert fake.calls_to("/file-urls/batch")[0].auth == f"Bearer {API_KEY}"
    assert fake.calls_to("extract-results")[0].auth == f"Bearer {API_KEY}"
    assert fake.calls_to("upload.example.com")[0].auth is None
    assert fake.calls_to("result.example.com")[0].auth is None


async def test_duplicate_file_names_are_correlated_by_data_id(tmp_path) -> None:
    """两个不同目录的同名 PDF 不能串结果。"""

    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    first_dir.mkdir()
    second_dir.mkdir()
    first = write(first_dir, "制度.pdf", b"%PDF-1.4 one")
    second = write(second_dir, "制度.pdf", b"%PDF-1.4 two")

    fake = FakeMinerU(
        zip_by_data_id={"0": make_zip("一等奖制度"), "1": make_zip("二等奖制度")}
    )

    result = await make_client(fake).parse_batch([first, second])

    assert result[str(first)] == "一等奖制度"
    assert result[str(second)] == "二等奖制度"


async def test_zip_is_saved_for_troubleshooting(tmp_path) -> None:
    fake = FakeMinerU()
    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")
    output_dir = tmp_path / "mineru-out"

    await make_client(fake, output_dir=str(output_dir)).parse_batch([pdf])

    assert (output_dir / "制度.zip").exists()


async def test_falls_back_to_first_markdown_when_full_md_absent(tmp_path) -> None:
    fake = FakeMinerU(zip_bytes=make_zip("备用正文", entry="output/document.md"))
    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")

    result = await make_client(fake).parse_batch([pdf])

    assert result[str(pdf)] == "备用正文"


# --- 失败路径 -------------------------------------------------------------


async def test_failed_state_raises_with_file_name(tmp_path) -> None:
    fake = FakeMinerU(poll_states=("failed",))
    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")

    with pytest.raises(MinerUError, match="制度.pdf"):
        await make_client(fake).parse_batch([pdf])


async def test_poll_timeout_raises(tmp_path) -> None:
    fake = FakeMinerU(poll_states=("running",))
    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")

    with pytest.raises(MinerUError, match="超时"):
        await make_client(fake, timeout=0).parse_batch([pdf])


async def test_business_error_code_raises(tmp_path) -> None:
    fake = FakeMinerU(business_code=1002)
    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")

    with pytest.raises(MinerUError, match="1002"):
        await make_client(fake).parse_batch([pdf])


async def test_http_error_on_submit_raises(tmp_path) -> None:
    fake = FakeMinerU(submit_status=503)
    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")

    with pytest.raises(MinerUError, match="503"):
        await make_client(fake).parse_batch([pdf])


async def test_upload_failure_raises(tmp_path) -> None:
    fake = FakeMinerU(upload_status=403)
    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")

    with pytest.raises(MinerUError, match="上传原件失败"):
        await make_client(fake).parse_batch([pdf])


async def test_missing_api_key_fails_before_any_request(tmp_path) -> None:
    fake = FakeMinerU()
    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")

    with pytest.raises(MinerUError, match="MINERU_API_KEY"):
        await make_client(fake, api_key="").parse_batch([pdf])

    assert fake.calls == []


async def test_zip_without_any_markdown_raises(tmp_path) -> None:
    fake = FakeMinerU(zip_bytes=make_zip(None))
    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")

    with pytest.raises(MinerUError, match="full.md"):
        await make_client(fake).parse_batch([pdf])


async def test_broken_zip_raises(tmp_path) -> None:
    fake = FakeMinerU(zip_bytes=b"not a zip")
    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")

    with pytest.raises(MinerUError, match="ZIP"):
        await make_client(fake).parse_batch([pdf])


@pytest.mark.parametrize(
    "fake",
    [
        FakeMinerU(business_code=1002),
        FakeMinerU(submit_status=503),
        FakeMinerU(poll_states=("failed",)),
        FakeMinerU(zip_bytes=b"not a zip"),
    ],
)
async def test_error_messages_never_leak_api_key(tmp_path, fake) -> None:
    """异常信息会写进 parse_error 并可能被前端展示，不能带密钥。"""

    pdf = write(tmp_path, "制度.pdf", b"%PDF-1.4 fake")

    with pytest.raises(MinerUError) as error:
        await make_client(fake).parse_batch([pdf])

    assert API_KEY not in str(error.value)
