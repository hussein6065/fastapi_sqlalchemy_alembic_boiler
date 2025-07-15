import json, redis
from typing import Callable
from datetime import datetime
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
import jwt
from pydantic import ValidationError

from ..core.config import settings
from ..schemas.tokens import TokenPayloadSchema
from ..schemas.user_deps import UserDepSchema, PermissionSchema
from ..database.get_session import get_async_session
from ..cruds.users import user_crud
from ..models.users import User
from ..models.roles import Role
from ..models.permissions import Permission
from ..models.role_permissions import RolePermission
from ..models.user_roles import UserRole
from ..utils.responses import (
    not_authorized_response,
    forbidden_response,
    internal_server_error_response,
)
from ..services.redis_base import client as redis_client
from ..core.loggers import app_logger as logger

reuseable_oauth = OAuth2PasswordBearer(
    tokenUrl="/api/v1/login/",
    scheme_name="JWT",
)


async def get_user_permissions(user: User, db: AsyncSession):
    """Fetch permissions for all roles associated with the user."""
    stmt = (
        select(Permission)
        .join(RolePermission, RolePermission.permission_uuid == Permission.uuid)
        .join(Role, Role.uuid == RolePermission.role_uuid)
        .join(UserRole, UserRole.role_uuid == Role.uuid)
        .join(User, User.uuid == UserRole.user_uuid)
        .where(User.uuid == user.uuid)
        .distinct(Permission.uuid)
    )
    result = await db.execute(stmt)
    permissions = result.scalars().all()
    return permissions


async def get_current_user(
    token: str = Depends(reuseable_oauth), session: Session = Depends(get_async_session)
):
    try:
        # Decode JWT
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        token_data = TokenPayloadSchema(**payload)

        # Check if token is expired
        if datetime.fromtimestamp(token_data.exp) < datetime.now():
            return forbidden_response("Token expired")

        cached_user = redis_client.get(f"token:{token}")
        if cached_user:
            user_info = json.loads(cached_user)
            return UserDepSchema(**user_info)

        user = await user_crud.get(
            db=session, uuid=token_data.sub, eager_load=[User.roles]
        )
        if user is None:
            return not_authorized_response("Could not validate credentials")

        permissions = await get_user_permissions(user, session)
        user_data = UserDepSchema(
            **user.to_orm_dict(),
            permissions=[
                PermissionSchema(**permission.to_dict()) for permission in permissions
            ],
        )
        expires_in = payload["exp"] - int(datetime.now().timestamp())  # Remaining time
        # Use 60 minutes (60*60 seconds) as default expiry, unless expires_in is less
        expiry_time = min(3600, expires_in) if expires_in > 0 else 3600
        redis_client.setex(f"token:{token}", expiry_time, user_data.model_dump_json())
        return user_data
    except ValidationError:
        return not_authorized_response("Could not validate credentials")
    except jwt.ExpiredSignatureError:
        logger.error("Token expired")
        return not_authorized_response("Token expired")
    except jwt.PyJWTError:
        logger.error("Invalid token")
        return not_authorized_response("Invalid token")
    except redis.exceptions.ConnectionError:
        logger.error("Redis connection error")
        return internal_server_error_response("Internal server error")
    except Exception as e:
        logger.error(f"Error authenticating user: {e}")
        return internal_server_error_response("Internal server error")


def get_user_with_role(required_role: str) -> Callable:
    """
    Dependency to check if a user has at least one of the required roles.

    :param required_role: Comma-separated list of required roles.
    :return: FastAPI dependency function.
    """

    async def role_dependency(user: UserDepSchema = Depends(get_current_user)):
        if not user.has_role(required_role):
            return forbidden_response(
                f"You do not have the required role(s): {required_role}"
            )
        return user

    return role_dependency


def get_user_with_permission(required_permission: str) -> Callable:
    """
    Dependency to check if a user has at least one of the required permissions.

    :param required_permission: Comma-separated list of required permissions.
    :return: FastAPI dependency function.
    """

    async def permission_dependency(user: UserDepSchema = Depends(get_current_user)):
        if not user.has_permission(required_permission):
            return forbidden_response(
                f"You do not have the required permission(s): {required_permission}"
            )
        return user

    return permission_dependency
