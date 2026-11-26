from fastapi import APIRouter

from pismor.admin.routers.config import router as config_router
from pismor.admin.routers.entries import router as entries_router
from pismor.admin.routers.merge_tasks import router as merge_tasks_router
from pismor.admin.routers.overview import router as overview_router
from pismor.admin.routers.review import router as review_router
from pismor.admin.routers.system import router as system_router
from pismor.admin.routers.taxonomy import router as taxonomy_router
from pismor.admin.routers.users import router as users_router


router = APIRouter()
router.include_router(overview_router)
router.include_router(review_router)
router.include_router(merge_tasks_router)
router.include_router(users_router)
router.include_router(entries_router)
router.include_router(system_router)
router.include_router(taxonomy_router)
router.include_router(config_router)
