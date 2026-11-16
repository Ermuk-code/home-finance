from datetime import date
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.models import User
from app.services.reporting import build_report_data
from app.services.screenshot import decode_render_token

router = APIRouter(tags=['Internal reports'])
templates = Jinja2Templates(directory=str(Path(__file__).parent/'templates'))


@router.get('/dashboard-render')
async def dashboard_render(request: Request, start_date: date, end_date: date,
                           report_type: Literal['finance','health'] = 'finance',
                           token: Optional[str] = Query(default=None), session: AsyncSession = Depends(get_async_session)):
    header = request.headers.get('Authorization','')
    credential = header[7:] if header.startswith('Bearer ') else token
    if not credential:
        raise HTTPException(401, 'Нужен временный токен отчета.')
    user_id = decode_render_token(credential,start_date,end_date,report_type)
    user = await session.get(User,user_id)
    if user is None:
        raise HTTPException(401, 'Профиль больше недоступен.')
    try:
        payload = await build_report_data(session,user,report_type,start_date,end_date)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    return templates.TemplateResponse(request=request,name='dashboard_render.html',context={'payload':payload},
        headers={'Cache-Control':'no-store, private','Referrer-Policy':'no-referrer',
                 'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; frame-ancestors 'none'"})
