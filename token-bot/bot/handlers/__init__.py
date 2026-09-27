from aiogram import Router

from bot.handlers import common, launch, trade


def build_router() -> Router:
    router = Router(name="root")
    # common first so /cancel and /start win over state-bound text handlers;
    # launch last because it ends with a catch-all for stale buttons.
    router.include_routers(common.router, trade.router, launch.router)
    return router
