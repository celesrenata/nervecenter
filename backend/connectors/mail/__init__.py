"""Mail connector plane (prompt2 §46) — package exports."""

from backend.api.models import MailConnector

from backend.connectors.mail.imap_client import (
    ImapClient,
    ImapImapClient,
    ImapProbeResult,
    ProbeResult,
    StubImapClient,
)
from backend.connectors.mail.oauth import (
    OauthCallback,
    OauthProvider,
    PROVIDERS,
    build_authorization_url,
    exchange_request,
    make_token_ref,
    parse_callback,
    provider,
    validate_token_ref,
)
from backend.connectors.mail.oauth_refresh import (
    ACCESS_TOKEN_KIND,
    MailOAuthRefreshError,
    MailOAuthRefresher,
    REFRESH_TOKEN_KIND,
    RefreshedTokens,
    exchange_refresh_token,
    stored_refresh_token,
)
from backend.connectors.mail.service import (
    MailConnectorNotFoundError,
    MailConnectorService,
    get_mail_connector_service,
    reset_mail_connector_service,
)

__all__ = [
    "MailConnector",
    "ImapClient",
    "ImapImapClient",
    "ImapProbeResult",
    "ProbeResult",
    "StubImapClient",
    "OauthCallback",
    "OauthProvider",
    "PROVIDERS",
    "build_authorization_url",
    "exchange_request",
    "make_token_ref",
    "parse_callback",
    "provider",
    "validate_token_ref",
    "MailConnectorNotFoundError",
    "MailConnectorService",
    "get_mail_connector_service",
    "reset_mail_connector_service",
    "MailOAuthRefreshError",
    "MailOAuthRefresher",
    "RefreshedTokens",
    "REFRESH_TOKEN_KIND",
    "ACCESS_TOKEN_KIND",
    "exchange_refresh_token",
    "stored_refresh_token",
]
