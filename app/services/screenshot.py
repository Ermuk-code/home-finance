"""Private, bounded screenshot generation. Render tokens never grant a web session."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException
from jose import JWTError, jwt

from app.core.config import settings
from app.services.reporting import validate_period

_render_gate = asyncio.Semaphore(1)
TOKEN_TYPE = 'dashboard_render'


def create_render_token(user_id: int, start: date, end: date, report_type: str = 'finance') -> str:
    validate_period(start, end)
    if report_type not in ('finance', 'health'):
        raise ValueError('Неизвестный тип отчета')
    return jwt.encode({'sub': str(user_id), 'typ': TOKEN_TYPE, 'start': start.isoformat(), 'end': end.isoformat(),
                       'kind': report_type, 'exp': datetime.now(timezone.utc)+timedelta(minutes=2)},
                      settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_render_token(token: str, start: date, end: date, report_type: str) -> int:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_id = int(payload['sub'])
        if (payload.get('typ'), payload.get('start'), payload.get('end'), payload.get('kind')) != (
            TOKEN_TYPE, start.isoformat(), end.isoformat(), report_type
        ) or user_id <= 0:
            raise ValueError
        return user_id
    except (JWTError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(401, 'Недействительный или просроченный токен отчета.') from exc


async def take_dashboard_screenshot(start_date: date, end_date: date, token: str, report_type: str = 'finance') -> bytes:
    validate_period(start_date, end_date)
    decode_render_token(token, start_date, end_date, report_type)
    # Lazy import keeps text reports usable if the browser runtime is unavailable.
    from playwright.async_api import async_playwright
    params = urlencode({'start_date': start_date.isoformat(), 'end_date': end_date.isoformat(), 'report_type': report_type})
    url = 'http://127.0.0.1:8000/dashboard-render?' + params
    async with asyncio.timeout(60):
        async with _render_gate:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    page = await browser.new_page(viewport={'width':1200,'height':1400},device_scale_factor=1,
                                                  extra_http_headers={'Authorization':'Bearer '+token})
                    async def allow_local_only(route):
                        parsed = urlsplit(route.request.url)
                        if parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port == 8000:
                            await route.continue_()
                        else:
                            await route.abort()
                    await page.route('**/*', allow_local_only)
                    response = await page.goto(url, wait_until='networkidle', timeout=30000)
                    if response is None or response.status != 200:
                        raise RuntimeError('Не удалось открыть страницу отчета')
                    await page.locator('html[data-render-ready="true"]').wait_for(timeout=15000)
                    return await page.locator('#report-card').screenshot(type='png',animations='disabled',timeout=10000)
                finally:
                    await browser.close()
