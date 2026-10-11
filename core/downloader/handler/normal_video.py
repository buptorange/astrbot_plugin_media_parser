"""普通视频直链下载处理器。"""

import asyncio
import os
import re
import uuid
from typing import Dict, Any, List, Optional

import aiohttp

from ...logger import logger

from ...constants import Config
from ...storage import cleanup_file
from ..budget import classify_limit_source, merge_limit_sources, resolve_max_bytes
from ..fileio import _wait_for_task_completion, run_blocking
from ..utils import process_gather_results, generate_cache_file_path
from .base import download_media_from_url


async def _decrypt_video(
    source_path: str,
    output_path: str,
    decryption_key: str,
    max_bytes: Optional[int],
) -> Dict[str, Any]:
    """仅从本地读取 CENC 视频，成功解密后原子提交可发送文件。"""
    temp_output = f"{output_path}.{uuid.uuid4().hex}.part.mp4"
    process = None
    spawn_task = None
    try:
        spawn_task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-xerror",
                "-protocol_whitelist",
                "file",
                "-decryption_key",
                decryption_key,
                "-i",
                source_path,
                # 只复制码流时，错误密钥也可能返回成功；首秒实际解码验证密钥。
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-t",
                "1",
                "-f",
                "null",
                "-",
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                temp_output,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        process = await asyncio.shield(spawn_task)
        await asyncio.wait_for(
            process.communicate(), timeout=Config.VIDEO_DOWNLOAD_TIMEOUT
        )
        if process.returncode != 0:
            return {"file_path": None, "error": "视频解密失败"}
        output_size = await run_blocking(os.path.getsize, temp_output)
        if output_size <= 0:
            return {"file_path": None, "error": "视频解密输出为空"}
        if output_size > resolve_max_bytes(max_bytes, is_video=True):
            return {
                "file_path": None,
                "size_mb": output_size / (1024 * 1024),
                "error": "视频解密输出超过下载硬限制",
                "limit_source": classify_limit_source(
                    max_bytes, is_video=True, observed_bytes=output_size
                ),
            }
        os.replace(temp_output, output_path)
        return {
            "file_path": os.path.normpath(output_path),
            "size_mb": output_size / (1024 * 1024),
        }
    except asyncio.TimeoutError:
        return {"file_path": None, "error": "视频解密超时"}
    except asyncio.CancelledError:
        raise
    except FileNotFoundError:
        return {"file_path": None, "error": "ffmpeg未找到或视频解密未生成输出"}
    except (OSError, ValueError):
        return {"file_path": None, "error": "视频解密处理失败"}
    finally:
        # 启动子进程期间也可能取消；先取得启动结果，再终止并等待释放文件。
        if spawn_task is not None and process is None:
            await _wait_for_task_completion(spawn_task)
            if not spawn_task.cancelled() and spawn_task.exception() is None:
                process = spawn_task.result()
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            reap_task = asyncio.create_task(process.communicate())
            await _wait_for_task_completion(reap_task)
            if not reap_task.cancelled():
                reap_task.exception()
        cleanup_file(temp_output)


async def download_video_to_cache(
    session: aiohttp.ClientSession,
    video_url: str,
    cache_dir: str,
    media_id: str,
    index: int = 0,
    headers: dict = None,
    proxy: str = None,
    max_bytes: Optional[int] = None,
    budget=None,
    decryption_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """下载视频到缓存目录

    Args:
        session: aiohttp会话
        video_url: 视频URL
        cache_dir: 缓存目录路径
        media_id: 媒体ID
        index: 索引
        headers: 请求头字典
        proxy: 代理地址（可选）
        decryption_key: CENC 的 32 位十六进制密钥；指定时仅返回成功解密的文件。

    Returns:
        包含file_path和size_mb的字典，失败时为None
    """
    if not cache_dir:
        return None
    if decryption_key is not None and (
        not isinstance(decryption_key, str)
        or not re.fullmatch(r"[0-9a-fA-F]{32}", decryption_key)
    ):
        return {"file_path": None, "error": "视频解密密钥格式无效"}

    logger.debug(f"开始下载视频: {video_url}, media_id={media_id}, index={index}")

    encrypted_path = None
    output_path = None
    decrypted = False

    def file_path_generator(content_type: str, url: str) -> str:
        nonlocal encrypted_path, output_path
        output_path = generate_cache_file_path(
            cache_dir=cache_dir,
            media_id=media_id,
            media_type="video",
            index=index,
            content_type="video/mp4" if decryption_key is not None else content_type,
            url=url,
        )
        if decryption_key is not None:
            encrypted_path = f"{output_path}.encrypted.mp4"
            return encrypted_path
        return output_path

    try:
        (
            file_path,
            size_mb,
            status_code,
            error,
            limit_source,
        ) = await download_media_from_url(
            session=session,
            media_url=video_url,
            file_path_generator=file_path_generator,
            is_video=True,
            headers=headers,
            proxy=proxy,
            max_bytes=max_bytes,
            budget=budget,
        )
        if file_path and decryption_key is not None:
            result = await _decrypt_video(
                file_path, output_path, decryption_key, max_bytes
            )
            decrypted = bool(result.get("file_path"))
            result["status_code"] = status_code
            return result
    finally:
        if encrypted_path is not None:
            try:
                encrypted_size = os.path.getsize(encrypted_path)
            except OSError:
                encrypted_size = 0
            cleanup_file(encrypted_path)
            if budget is not None and encrypted_size and not decrypted:
                release_task = asyncio.create_task(budget.release(encrypted_size))
                await _wait_for_task_completion(release_task)
                if not release_task.cancelled():
                    release_task.exception()

    if file_path:
        logger.debug(f"视频下载完成: {video_url} -> {file_path}, {size_mb}MB")
        return {"file_path": file_path, "size_mb": size_mb, "status_code": status_code}
    logger.debug(f"视频下载失败: {video_url}")
    return {
        "file_path": None,
        "size_mb": size_mb,
        "status_code": status_code,
        "error": error or "下载失败",
        "limit_source": limit_source,
    }


async def batch_download_videos(
    session: aiohttp.ClientSession,
    video_items: List[Dict[str, Any]],
    cache_dir: str,
    max_concurrent: int = None,
    max_bytes: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """批量下载普通视频到缓存目录

    Args:
        session: aiohttp会话
        video_items: 视频项列表，每个项包含url_list（URL列表）、media_id、index、
            headers、proxy等字段
        cache_dir: 缓存目录路径
        max_concurrent: 最大并发下载数

    Returns:
        下载结果列表，每个项包含url（第一个URL）、file_path、success、index等字段
    """
    if not cache_dir or not video_items:
        return []

    if max_concurrent is None:
        max_concurrent = Config.DOWNLOAD_MANAGER_MAX_CONCURRENT
    semaphore = asyncio.Semaphore(max_concurrent)

    async def download_one(item: Dict[str, Any]) -> Dict[str, Any]:
        """下载单条普通视频并返回处理后的元数据。"""
        async with semaphore:
            try:
                url_list = item.get("url_list", [])
                media_id = item.get("media_id", "media")
                index = item.get("index", 0)
                item_headers = item.get("headers", {})
                item_proxy = item.get("proxy")

                if not url_list or not isinstance(url_list, list):
                    return {
                        "url": url_list[0] if url_list else None,
                        "file_path": None,
                        "success": False,
                        "index": index,
                    }

                last_result: Dict[str, Any] = {}
                failed_results: List[Dict[str, Any]] = []
                for url in url_list:
                    result = await download_video_to_cache(
                        session,
                        url,
                        cache_dir,
                        media_id,
                        index,
                        item_headers,
                        item_proxy,
                        max_bytes,
                    )
                    last_result = result or {}
                    if result and result.get("file_path"):
                        return {
                            "url": url_list[0],
                            "file_path": result.get("file_path"),
                            "size_mb": result.get("size_mb"),
                            "success": True,
                            "index": index,
                        }
                    if result:
                        failed_results.append(result)

                limited_results = [
                    result for result in failed_results if result.get("limit_source")
                ]
                known_sizes = [
                    float(result["size_mb"])
                    for result in limited_results
                    if isinstance(result.get("size_mb"), (int, float))
                ]
                error_result = next(
                    (result for result in limited_results if result.get("error")),
                    last_result,
                )

                return {
                    "url": url_list[0] if url_list else None,
                    "file_path": None,
                    "size_mb": max(known_sizes) if known_sizes else None,
                    "status_code": last_result.get("status_code"),
                    "success": False,
                    "index": index,
                    "error": error_result.get("error"),
                    "limit_source": merge_limit_sources(
                        *(result.get("limit_source") for result in failed_results)
                    ),
                }
            except Exception as e:
                url_list = item.get("url_list", [])
                index = item.get("index", 0)
                logger.warning(
                    f"批量下载视频失败: {url_list[0] if url_list else 'unknown'}, 错误: {e}"
                )
                return {
                    "url": url_list[0] if url_list else None,
                    "file_path": None,
                    "success": False,
                    "index": index,
                    "error": str(e),
                }

    tasks = [download_one(item) for item in video_items]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return process_gather_results(results, video_items)
