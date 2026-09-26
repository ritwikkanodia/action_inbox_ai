"""Explicit production configuration. Test fixtures construct their own values."""
from dataclasses import dataclass, field
import ipaddress
import re
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from psycopg.conninfo import conninfo_to_dict


def required(values, key):
    value = values.get(key)
    if not isinstance(value, str) or not value or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise ValueError('invalid_cloud_configuration')
    return value


@dataclass(frozen=True)
class DatabaseSettings:
    dsn: str = field(repr=False)
    schema: str

    @classmethod
    def from_mapping(cls, values):
        dsn = required(values, 'ATHENA_DATABASE_URL')
        schema = required(values, 'ATHENA_DATABASE_SCHEMA')
        try:
            fields = conninfo_to_dict(dsn)
            host = fields.get('host', '')
            if (not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,62}', schema)
                    or not fields.get('dbname') or not fields.get('user')
                    or fields.get('sslmode') != 'verify-full'
                    or any(k in fields for k in ('options', 'service', 'hostaddr'))
                    or not re.fullmatch(r'[a-zA-Z0-9.-]+', host) or host.lower() == 'localhost'):
                raise ValueError()
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address and (address.is_loopback or address.is_unspecified):
                raise ValueError()
        except Exception:
            raise ValueError('invalid_cloud_database_configuration') from None
        return cls(dsn, schema)


@dataclass(frozen=True)
class WebSettings:
    database: DatabaseSettings
    environment: str
    public_origin: str
    google_client_id: str
    google_client_secret: str = field(repr=False)
    flow_key: bytes = field(repr=False)

    @classmethod
    def from_mapping(cls, values):
        database = DatabaseSettings.from_mapping(values)
        environment = required(values, 'ATHENA_ENVIRONMENT')
        origin = required(values, 'ATHENA_PUBLIC_ORIGIN')
        client_id = required(values, 'ATHENA_GOOGLE_CLIENT_ID')
        secret = required(values, 'ATHENA_GOOGLE_CLIENT_SECRET')
        try:
            parsed = urlsplit(origin)
            if (environment not in ('staging', 'production') or parsed.scheme != 'https'
                    or not parsed.hostname or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment or not origin.isascii()
                    or not re.fullmatch(r'https://[a-z0-9.-]+(?::[0-9]+)?', origin)
                    or parsed.port == 0):
                raise ValueError()
            key = required(values, 'ATHENA_AUTH_FLOW_KEY').encode('ascii')
            Fernet(key)
        except Exception:
            raise ValueError('invalid_cloud_web_configuration') from None
        return cls(database, environment, origin, client_id, secret, key)

