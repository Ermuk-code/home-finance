"""Питание в боте: привязка/отвязка FatSecret и итоги за сегодня (только свои данные)."""
from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.base import get_or_create_user
from app.health import fatsecret_client
from app.health.nutrition_sync import local_today, sync_user

router = Router(name="nutrition_router")


@router.message(Command("link_fatsecret"))
async def cmd_link_fatsecret(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(session, message.from_user.id)
    if not fatsecret_client.is_configured():
        await message.answer("⚠️ FatSecret не настроен на сервере (FATSECRET_CONSUMER_KEY / FATSECRET_CONSUMER_SECRET).")
        return
    try:
        url = await fatsecret_client.start_link(user.id)
    except fatsecret_client.FatSecretError as e:
        await message.answer(f"⚠️ Не удалось получить ссылку авторизации: {escape(str(e))}", parse_mode="HTML")
        return
    note = "Аккаунт уже привязан — ссылка перепривяжет его.\n\n" if user.fs_linked else ""
    await message.answer(
        f"🍎 <b>Привязка FatSecret</b>\n{note}"
        "Откройте ссылку, войдите в свой FatSecret и нажмите «Разрешить». Ссылка действует 15 минут.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Привязать FatSecret", url=url)]]),
    )


@router.message(Command("unlink_fatsecret"))
async def cmd_unlink_fatsecret(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(session, message.from_user.id)
    if not user.fs_linked:
        await message.answer("FatSecret не привязан.")
        return
    user.fs_access_token = None
    user.fs_access_secret = None
    await session.commit()
    await message.answer("🔌 FatSecret отвязан. Накопленные данные о питании сохранены.")


@router.message(Command("nutrition"))
async def cmd_nutrition(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return
    user = await get_or_create_user(session, message.from_user.id)
    if not user.fs_linked:
        await message.answer("FatSecret не привязан. Команда: /link_fatsecret")
        return
    try:
        totals = await sync_user(session, user, local_today())
    except Exception as e:  # noqa: BLE001
        await session.rollback()
        await message.answer(f"⚠️ Не удалось получить данные FatSecret: {escape(str(e))}", parse_mode="HTML")
        return
    await message.answer(
        f"🍎 <b>Питание за сегодня</b>\n"
        f"Калории: <b>{totals.calories} ккал</b>\n"
        f"Белки: {totals.protein:.0f} г · Жиры: {totals.fat:.0f} г · Углеводы: {totals.carbs:.0f} г",
        parse_mode="HTML",
    )
