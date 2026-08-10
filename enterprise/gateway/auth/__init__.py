"""Enterprise Gateway authentication — service and end-user auth."""
from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.auth.middleware import require_user_principal
from enterprise.gateway.auth.service_auth import (
    CredentialBinding,
    CredentialIdentity,
    require_service_principal,
)
from enterprise.gateway.auth.service_principal import ServicePrincipal
from enterprise.gateway.auth.token_validator import JWTValidator, JWTValidatorConfig, TokenValidationError

__all__ = [
    "require_service_principal",
    "require_user_principal",
    "CredentialBinding",
    "CredentialIdentity",
    "ServicePrincipal",
    "UserPrincipal",
    "JWTValidator",
    "JWTValidatorConfig",
    "TokenValidationError",
]
