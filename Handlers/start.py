from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from Handlers.menus import main_menu
from app_context import AppContext


router = Router(name="start")


@router.message(CommandStart())
async def start_handler(message: Message, app_context: AppContext, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.clear()
    app_context.state.reset_conversation(message.chat.id, message.from_user.id)
    await message.answer(
        "👋 Привет! Я AiS.\n\nВыберите модель и начните разговор. Ваш доступ и настройки сохраняются автоматически.",
        reply_markup=main_menu(),
    )


@router.message(Command("menu", "cancel"))
async def menu_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("AiS · Главное меню", reply_markup=main_menu())
