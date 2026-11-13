from fastapi import APIRouter

from app.admin_deps import FullSession
from app.rate_limit import rejection_stats

router = APIRouter(prefix="/admin/api", tags=["admin-rate-limit"])


@router.get("/rate-limit")
async def get_rate_limit(_sess: FullSession):
    """The /validate rate limit and the requests it rejected.

    Counts are kept in memory by the server process that answers this request,
    since that process started; a restart resets them.
    """
    return rejection_stats()
