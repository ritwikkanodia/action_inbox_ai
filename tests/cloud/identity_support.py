"""Synthetic identity configuration only; never reads local credentials."""
from cryptography.fernet import Fernet


def test_settings():
    return {
        'ATHENA_DATABASE_URL': 'postgresql://fixture:synthetic@db.example.invalid/athena?sslmode=verify-full',
        'ATHENA_DATABASE_SCHEMA': 'athena',
        'ATHENA_ENVIRONMENT': 'staging',
        'ATHENA_PUBLIC_ORIGIN': 'https://athena.example.invalid',
        'ATHENA_GOOGLE_CLIENT_ID': 'fixture.apps.googleusercontent.com',
        'ATHENA_GOOGLE_CLIENT_SECRET': 'synthetic-client-secret',
        'ATHENA_AUTH_FLOW_KEY': Fernet.generate_key().decode(),
    }

