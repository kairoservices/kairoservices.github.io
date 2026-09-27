from aiogram import Router

from bot.handlers import common, launch


def build_router() -> Router:
    router = Router(name="root")
    # common first so /cancel and /start win over state-bound text handlers.
    router.include_routers(common.router, launch.router)
    return router
