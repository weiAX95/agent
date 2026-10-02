"""
PDF 解析器：基于 MinerU HTTP API 的封装

问题背景：
    MinerU 3.4.5 的 CLI 在本机（macOS）会因为 httpx 读取系统代理设置，
    导致与本地 mineru-api 通信时出现 502 Bad Gateway。
    本脚本绕过 mineru CLI，直接：
      1. 启动本地 mineru-api 服务
      2. 通过 HTTP API 提交解析任务
      3. 轮询任务状态
      4. 下载 ZIP 结果并解压
      5. 关闭本地服务

依赖：
    已在 .venv 中安装 mineru，并可用 .venv/bin/python 运行。

运行：
    cd /Users/zhangweiwei/Desktop/code/agent
    source .venv/bin/activate
    python pdf_parser.py
"""

import atexit
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

import requests

# 脚本所在目录，用于解析相对路径
SCRIPT_DIR = Path(__file__).resolve().parent
# 项目根目录（src/parsers/pdf_parser.py → src/parsers → src → 根目录）
PROJECT_ROOT = SCRIPT_DIR.parent.parent


def _find_free_port() -> int:
    """找一个可用的本地端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _session_without_proxy() -> requests.Session:
    """创建一个禁用系统代理的 requests Session。"""
    session = requests.Session()
    session.trust_env = False
    return session


class MinerUAPIServer:
    """管理本地 MinerU API 服务的启动和停止。"""

    def __init__(self, port: Optional[int] = None):
        self.port = port or _find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process: Optional[subprocess.Popen] = None
        self.log_file: Optional[Path] = None

    def start(self, timeout: float = 60.0) -> str:
        """启动 mineru-api 并等待健康检查通过。"""

        cmd = [
            sys.executable,
            "-m",
            "mineru.cli.fast_api",
            "--port",
            str(self.port),
        ]

        self.log_file = PROJECT_ROOT / "output" / "mineru_api.log"
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        print(f"[MinerU] 启动本地 API: {self.base_url}")
        print(f"[MinerU] API 日志: {self.log_file}")

        # stdin=PIPE 避免子进程因 EOF 而自动关闭
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=open(self.log_file, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            cwd=str(SCRIPT_DIR),
        )

        # 注册退出时清理
        atexit.register(self.stop)

        # 等待服务健康
        session = _session_without_proxy()
        deadline = time.time() + timeout
        last_error = None

        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"mineru-api 提前退出，退出码: {self.process.returncode}"
                )
            try:
                resp = session.get(
                    f"{self.base_url}/health",
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "healthy":
                        print(f"[MinerU] API 已就绪")
                        return self.base_url
                    last_error = f"API 状态异常: {data}"
                else:
                    last_error = f"HTTP {resp.status_code}"
            except Exception as exc:
                last_error = str(exc)

            time.sleep(0.5)

        self.stop()
        raise RuntimeError(
            f"等待 mineru-api 就绪超时: {last_error}"
        )

    def stop(self) -> None:
        """停止 mineru-api 服务。"""
        if self.process is None:
            return

        print("[MinerU] 关闭本地 API...")
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        except Exception as exc:
            print(f"[MinerU] 关闭 API 时出错: {exc}")
        finally:
            self.process = None


class PDFParser:
    def __init__(self, output_dir: Optional[Path] = None):
        """
        output_dir: 输出目录，默认放在脚本同级的 output/ 下
        """
        if output_dir is None:
            self.output_dir = PROJECT_ROOT / "output"
        else:
            self.output_dir = Path(output_dir).expanduser().resolve()

        self.output_dir.mkdir(parents=True, exist_ok=True)

    def parse(
        self,
        pdf_path: str,
        backend: str = "pipeline",
        effort: str = "medium",
        lang: str = "ch",
        parse_method: str = "auto",
        formula_enable: bool = True,
        table_enable: bool = True,
        return_images: bool = True,
        timeout_minutes: float = 30.0,
    ) -> dict:
        """
        解析 PDF，返回结果文件路径字典。

        Returns:
            {
                "pdf": 原始 PDF 路径,
                "markdown": Markdown 文件路径,
                "content_json": content_list JSON 路径,
                "middle_json": middle JSON 路径,
                "images": 图片目录路径,
                "output_dir": 输出目录,
            }
        """

        pdf_path = Path(pdf_path)
        if not pdf_path.is_absolute():
            pdf_path = PROJECT_ROOT / pdf_path
        pdf_path = pdf_path.resolve()

        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF 不存在: {pdf_path}")
        if pdf_path.suffix.lower() != ".pdf":
            raise ValueError(f"只支持 PDF 文件: {pdf_path}")

        # 启动 API
        server = MinerUAPIServer()
        base_url = server.start()
        session = _session_without_proxy()

        try:
            # 提交任务
            print(f"[Parser] 开始解析: {pdf_path.name}")
            print(f"[Parser] 后端: {backend}, 语言: {lang}")

            with open(pdf_path, "rb") as f:
                files = {
                    "files": (pdf_path.name, f, "application/pdf"),
                }
                data = {
                    "lang_list": lang,
                    "backend": backend,
                    "effort": effort,
                    "parse_method": parse_method,
                    "formula_enable": "true" if formula_enable else "false",
                    "table_enable": "true" if table_enable else "false",
                    "image_analysis": "true",
                    "return_md": "true",
                    "return_content_list": "true",
                    "return_middle_json": "true",
                    "return_model_output": "false",
                    "return_images": "true" if return_images else "false",
                    "response_format_zip": "true",
                    "return_original_file": "false",
                    "client_side_output_generation": "false",
                }

                resp = session.post(
                    f"{base_url}/tasks",
                    files=files,
                    data=data,
                    timeout=60.0,
                )

            if resp.status_code != 202:
                raise RuntimeError(
                    f"提交任务失败: {resp.status_code} {resp.text}"
                )

            task = resp.json()
            task_id = task["task_id"]
            print(f"[Parser] 任务已提交: {task_id}")

            # 轮询任务状态（带短暂重试，避免 API 处理高峰时连接被重置）
            status_url = f"{base_url}/tasks/{task_id}"
            result_url = f"{base_url}/tasks/{task_id}/result"
            deadline = time.time() + timeout_minutes * 60

            consecutive_errors = 0
            while time.time() < deadline:
                if server.process is not None and server.process.poll() is not None:
                    raise RuntimeError(
                        f"mineru-api 在处理过程中退出，退出码: {server.process.returncode}"
                    )

                try:
                    resp = session.get(status_url, timeout=30.0)
                    resp.raise_for_status()
                    consecutive_errors = 0
                except requests.RequestException as exc:
                    consecutive_errors += 1
                    print(f"[Parser] 查询状态失败 ({consecutive_errors}/5): {exc}")
                    if consecutive_errors >= 5:
                        raise RuntimeError("连续多次查询任务状态失败") from exc
                    time.sleep(2.0)
                    continue

                status = resp.json()
                state = status.get("status")
                print(f"[Parser] 任务状态: {state}")

                if state == "completed":
                    break
                if state == "failed":
                    error = status.get("error") or "未知错误"
                    raise RuntimeError(f"解析任务失败: {error}")

                time.sleep(2.0)
            else:
                raise RuntimeError("等待解析完成超时")

            # 下载结果 ZIP
            print("[Parser] 下载解析结果...")
            resp = session.get(result_url, timeout=120.0)
            resp.raise_for_status()

            zip_path = self.output_dir / f"{pdf_path.stem}_result.zip"
            with open(zip_path, "wb") as f:
                f.write(resp.content)

            print(f"[Parser] 结果已下载: {zip_path} ({len(resp.content)} bytes)")

            # 解压
            extract_dir = self.output_dir / pdf_path.stem
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
            extract_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(extract_dir)

            print(f"[Parser] 结果已解压到: {extract_dir}")

            return self._find_output(pdf_path, extract_dir)

        finally:
            server.stop()

    def _find_output(self, pdf_path: Path, extract_dir: Path) -> dict:
        """在解压后的目录里查找 markdown、json、images。"""

        result = {
            "pdf": str(pdf_path),
            "markdown": None,
            "content_json": None,
            "middle_json": None,
            "images": None,
            "output_dir": str(extract_dir),
        }

        # 查找 markdown
        md_files = list(extract_dir.rglob("*.md"))
        if md_files:
            result["markdown"] = str(md_files[0])

        # 查找 content_list.json
        content_jsons = list(extract_dir.rglob("*content_list.json"))
        if content_jsons:
            result["content_json"] = str(content_jsons[0])

        # 查找 middle.json
        middle_jsons = list(extract_dir.rglob("*middle.json"))
        if middle_jsons:
            result["middle_json"] = str(middle_jsons[0])

        # 查找 images 目录
        images_dirs = list(extract_dir.rglob("images"))
        for d in images_dirs:
            if d.is_dir():
                result["images"] = str(d)
                break

        return result


def _pretty_print(result: dict) -> None:
    """美观地打印解析结果。"""
    print("\n========== PDF 解析结果 ==========")
    for key in ["pdf", "markdown", "content_json", "middle_json", "images", "output_dir"]:
        value = result.get(key)
        print(f"{key:15s}: {value}")
    print("==================================\n")


if __name__ == "__main__":
    parser = PDFParser()
    result = parser.parse(str(PROJECT_ROOT / "docs" / "test.pdf"))
    _pretty_print(result)
