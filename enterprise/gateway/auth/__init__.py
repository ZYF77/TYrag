"""Enterprise Gateway authentication — service and future user auth."""
from enterprise.gateway.auth.service_auth import require_service_principal
from enterprise.gateway.auth.service_principal import ServicePrincipal

__all__ = ["require_service_principal", "ServicePrincipal"]
