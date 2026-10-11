"""README 页面截图渲染器，截取解析器提供的正文区域并按需分页。"""

import asyncio
import math
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import List
from urllib.parse import unquote, urlsplit


PAGE_WIDTH = 1080
PAGE_HEIGHT = 1800
MAX_RENDER_HEIGHT = 21600


async def render_readme_images(
    url: str,
    selector: str,
    output_path: str,
    *,
    paginate: bool = False,
    proxy_url: str = '',
    timeout_seconds: int = 60,
) -> List[str]:
    """截取页面中的 README 正文，完整保留限长范围内的内容。

    Args:
        url: 解析器校验后的规范仓库首页地址。
        selector: 解析器提供的 README 正文 CSS 选择器。
        output_path: 输出 PNG 路径，多页时追加页码后缀。
        paginate: 是否将长图拆分为多张图片，默认保留单张输出。
        proxy_url: 页面及其资源请求使用的代理地址。
        timeout_seconds: 包含浏览器启动和正文加载的总超时时间。

    Returns:
        按阅读顺序排列的本地 PNG 绝对路径。

    Raises:
        RuntimeError: 浏览器不可用、正文缺失、页面超时或截图失败。
        ValueError: 截图地址、选择器或超时参数无效。
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise ValueError('README 截图地址必须是完整 HTTP 链接')
    if not isinstance(selector, str) or not selector.strip():
        raise ValueError('README 正文选择器不能为空')
    timeout = max(1, float(timeout_seconds))
    if not math.isfinite(timeout):
        raise ValueError('README 截图超时时间必须是有限数值')
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    generated: List[Path] = []
    complete = False
    try:
        paths = await asyncio.wait_for(
            _render_pages(url, selector, output, proxy_url, generated, paginate),
            timeout=timeout,
        )
        if not paths or any(not Path(path).is_file() or Path(path).stat().st_size <= 8 for path in paths):
            raise RuntimeError('README 截图未生成有效图片')
        for path in paths:
            with Path(path).open('rb') as image:
                if image.read(8) != b'\x89PNG\r\n\x1a\n':
                    raise RuntimeError('README 截图未生成有效 PNG 图片')
        complete = True
        return paths
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        raise RuntimeError('README 页面截图超时，请稍后再试') from exc
    finally:
        if not complete:
            for path in generated:
                with suppress(OSError):
                    path.unlink(missing_ok=True)


async def _render_pages(
    url: str,
    selector: str,
    output: Path,
    proxy_url: str,
    generated: List[Path],
    paginate: bool,
) -> List[str]:
    """管理一次无头浏览器生命周期，只截取平台声明的正文区域。"""
    try:
        from playwright.async_api import Error, TimeoutError, async_playwright
    except ImportError as exc:
        raise RuntimeError('README 截图需要安装 Playwright 及 Chromium 浏览器') from exc

    browser = None
    rejected_navigation = False
    try:
        async with _playwright_runtime(async_playwright) as playwright:
            try:
                launch_options = {'headless': True}
                if proxy_url:
                    launch_options['proxy'] = _browser_proxy(proxy_url)
                browser = await playwright.chromium.launch(**launch_options)
                page = await browser.new_page(
                    viewport={'width': PAGE_WIDTH, 'height': PAGE_HEIGHT},
                    device_scale_factor=1,
                    color_scheme='light',
                    locale='zh-CN',
                )
                async def guard_navigation(route):
                    nonlocal rejected_navigation
                    request = route.request
                    target = urlsplit(request.url)
                    if request.is_navigation_request() and request.frame == page.main_frame and (
                        target.scheme not in {'http', 'https'}
                        or target.hostname != urlsplit(url).hostname
                        or target.username is not None
                    ):
                        rejected_navigation = True
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route('**/*', guard_navigation)
                response = await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                destination = urlsplit(page.url)
                if (
                    destination.scheme not in {'http', 'https'}
                    or destination.hostname != urlsplit(url).hostname
                    or destination.username is not None
                ):
                    raise RuntimeError('README 页面跳转到了非预期站点，已停止截图')
                if response is not None and response.status >= 400:
                    raise RuntimeError(f'README 页面请求失败（HTTP {response.status}）')
                readme = page.locator(selector).first
                try:
                    await readme.wait_for(state='visible', timeout=15000)
                    await page.wait_for_function('''selector => {
                        const root = document.querySelector(selector);
                        return root && (root.innerText.trim() || root.querySelector('img, svg'));
                    }''', arg=selector, timeout=15000)
                except TimeoutError as exc:
                    raise RuntimeError('未找到可截图的 README 正文，仓库可能未提供 README 或页面访问受限') from exc
                # 先等待资源，避免站点在加载期间重建正文时丢失截图标记。
                await page.evaluate('''async selector => {
                    const root = document.querySelector(selector);
                    if (!root) return;
                    const images = [...root.querySelectorAll('img')];
                    for (const image of images) image.loading = 'eager';
                    await Promise.resolve();
                    const resources = images.map(image => image.complete ? Promise.resolve() :
                        new Promise(resolve => {
                            image.addEventListener('load', resolve, {once: true});
                            image.addEventListener('error', resolve, {once: true});
                        }));
                    resources.push(document.fonts.ready);
                    await Promise.race([Promise.all(resources), new Promise(resolve => setTimeout(resolve, 6000))]);
                }''', selector)
                # 资源就绪后冻结页面脚本，防止站点在排版或分页截图时再次重建正文。
                page_session = await page.context.new_cdp_session(page)
                await page_session.send('Emulation.setScriptExecutionDisabled', {'value': True})
                css = '''
                    html, body { margin: 0 !important; padding: 0 !important;
                        width: 1080px !important; min-width: 0 !important;
                        min-height: 0 !important; height: auto !important;
                        background: #fff !important; color: #1f2328 !important; }
                    [data-media-parser-ancestor] { display: block !important;
                        position: static !important; width: 1080px !important;
                        min-width: 0 !important; max-width: none !important;
                        min-height: 0 !important; height: auto !important;
                        max-height: none !important; margin: 0 !important;
                        padding: 0 !important; border: 0 !important;
                        overflow: visible !important; transform: none !important;
                        contain: none !important; }
                    [data-media-parser-readme] { display: block !important;
                        position: relative !important; width: 1080px !important;
                        min-width: 0 !important; max-width: 1080px !important;
                        height: auto !important; max-height: none !important;
                        margin: 0 !important; padding: 32px !important;
                        box-sizing: border-box !important; overflow: visible !important;
                        background: #fff !important; color: #1f2328 !important; }
                    [data-media-parser-readme] * { box-sizing: border-box;
                        min-width: 0 !important; max-width: 100% !important;
                        overflow-wrap: anywhere; }
                    [data-media-parser-readme] pre,
                    [data-media-parser-readme] pre code {
                        white-space: pre-wrap !important; overflow: visible !important;
                        overflow-wrap: anywhere !important; word-break: break-word; }
                    [data-media-parser-readme] table { display: table !important;
                        width: 100% !important; table-layout: fixed !important; }
                    [data-media-parser-readme] td, [data-media-parser-readme] th {
                        white-space: normal !important; overflow-wrap: anywhere; }
                    [data-media-parser-readme] img { height: auto !important;
                        max-width: 100% !important; object-fit: contain; }
                    [data-media-parser-readme] details { max-height: none !important; }
                    .media-parser-readme-notice { padding: 20px 32px; margin: 0;
                        box-sizing: border-box; color: #5c4714; background: #fff5ce;
                        font: 18px/1.5 sans-serif; overflow-wrap: anywhere; }
                '''
                # 重新定位当前正文，在同一同步任务内完成隔离、排版和测量。
                layout_handle = await page.evaluate_handle('''({selector, css, maxHeight}) => {
                    const root = document.querySelector(selector);
                    if (!root || (!root.innerText.trim() && !root.querySelector('img, svg'))) {
                        return null;
                    }
                    const style = getComputedStyle(root);
                    root.style.fontFamily = style.fontFamily;
                    root.style.fontSize = style.fontSize;
                    root.style.lineHeight = style.lineHeight;
                    root.setAttribute('data-media-parser-readme', '');
                    let child = root;
                    while (child.parentElement && child.parentElement !== document.documentElement) {
                        const parent = child.parentElement;
                        for (const sibling of parent.children) {
                            if (sibling !== child) sibling.style.setProperty('display', 'none', 'important');
                        }
                        parent.setAttribute('data-media-parser-ancestor', '');
                        child = parent;
                    }
                    const sheet = document.createElement('style');
                    sheet.textContent = css;
                    document.head.append(sheet);
                    const missingImages = [...root.querySelectorAll('img')]
                        .filter(image => !image.complete || !image.naturalWidth).length;
                    const notices = [];
                    if (missingImages) notices.push('部分配图未能加载，请访问仓库查看完整 README。');
                    if (root.getBoundingClientRect().height > maxHeight - 150) {
                        root.style.setProperty('max-height', (maxHeight - 150) + 'px', 'important');
                        root.style.setProperty('overflow', 'hidden', 'important');
                        notices.push('README 较长，截图已达到长度上限；后续内容请访问仓库查看。');
                    }
                    if (notices.length) {
                        const notice = document.createElement('div');
                        notice.className = 'media-parser-readme-notice';
                        notice.textContent = notices.join(' ');
                        document.body.append(notice);
                    }
                    const height = Math.ceil(document.body.getBoundingClientRect().height);
                    const breaks = [...root.querySelectorAll('p, li, h1, h2, h3, h4, h5, h6, pre, tr, img')]
                        .map(element => Math.ceil(element.getBoundingClientRect().bottom))
                        .filter(bottom => bottom > 0 && bottom < height);
                    return {height, breaks, root};
                }''', {'selector': selector, 'css': css, 'maxHeight': MAX_RENDER_HEIGHT})
                layout = await layout_handle.evaluate('value => value && ({height: value.height, breaks: value.breaks})')
                if layout is None:
                    raise RuntimeError('README 正文在截图前变为空，已停止生成空白图片')
                height = int(layout['height'])
                if height <= 0:
                    raise RuntimeError('README 正文没有可截图的可见区域')
                spans = _page_spans(height, layout['breaks']) if paginate else [(0, height)]
                for index, (top, page_height) in enumerate(spans, 1):
                    path = output if len(spans) == 1 else output.with_name(f'{output.stem}_p{index:02d}.png')
                    generated.append(path)
                    await page.screenshot(
                        path=str(path), type='png', full_page=True,
                        clip={'x': 0, 'y': top, 'width': PAGE_WIDTH, 'height': page_height},
                        animations='disabled', timeout=10000,
                    )
                    if not await layout_handle.evaluate(
                        '(value, selector) => value.root.isConnected && value.root === document.querySelector(selector)',
                        selector,
                    ):
                        raise RuntimeError('README 正文在截图期间发生变化，请稍后再试')
                await layout_handle.dispose()
                return [str(path) for path in generated]
            finally:
                if browser is not None:
                    with suppress(Error):
                        await browser.close()
    except asyncio.CancelledError:
        raise
    except Error as exc:
        if rejected_navigation:
            raise RuntimeError('README 页面跳转到了非预期站点，已停止截图') from exc
        if 'Executable doesn\'t exist' in str(exc) or 'playwright install' in str(exc):
            raise RuntimeError('README 截图缺少 Chromium，请在 AstrBot 环境执行 python -m playwright install --with-deps chromium') from exc
        raise RuntimeError('README 页面加载或截图失败，请检查页面可访问性和代理设置') from exc


@asynccontextmanager
async def _playwright_runtime(factory):
    """启动后再响应取消，确保驱动连接始终进入对应的关闭流程。"""
    runtime = None
    startup = asyncio.create_task(factory().start())
    try:
        try:
            runtime = await asyncio.shield(startup)
        except asyncio.CancelledError:
            runtime = await startup
            raise
        yield runtime
    finally:
        if runtime is not None:
            await runtime.stop()


def _browser_proxy(proxy_url: str) -> dict:
    """分离代理认证字段，不把用户名和密码留在浏览器服务地址中。"""
    parsed = urlsplit(proxy_url)
    if parsed.scheme not in {'http', 'https', 'socks5'} or not parsed.hostname:
        raise ValueError('README 截图代理地址无效')
    hostname = parsed.hostname
    if ':' in hostname:
        hostname = '[' + hostname + ']'
    server = parsed.scheme + '://' + hostname
    if parsed.port is not None:
        server += ':' + str(parsed.port)
    proxy = {'server': server}
    if parsed.username is not None:
        proxy['username'] = unquote(parsed.username)
        proxy['password'] = unquote(parsed.password or '')
    return proxy


def _page_spans(height: int, breaks: List[int]) -> List[tuple[int, int]]:
    """尽量在段落末尾分页，超长单段仍保持完整、连续的像素范围。"""
    spans = []
    top = 0
    while top < height:
        bottom = min(top + PAGE_HEIGHT, height)
        if bottom < height:
            candidates = [value for value in breaks if bottom - 160 <= value <= bottom]
            if candidates:
                bottom = max(candidates)
        spans.append((top, bottom - top))
        top = bottom
    return spans
