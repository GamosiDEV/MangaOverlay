"""Chaves de API no chaveiro do sistema (GNOME Keyring / Windows Credential Manager)."""

import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from . import APP_NAME

OPENAI = "openai_api_key"
ANTHROPIC = "anthropic_api_key"

# Variável de ambiente usada quando a chave não está no chaveiro
_ENV_FALLBACK = {OPENAI: "OPENAI_API_KEY", ANTHROPIC: "ANTHROPIC_API_KEY"}


def get_key(name: str) -> str:
    try:
        key = keyring.get_password(APP_NAME, name)
    except KeyringError:
        key = None
    return key or os.environ.get(_ENV_FALLBACK[name], "")


def set_key(name: str, value: str) -> None:
    """Salva a chave; string vazia remove. Propaga KeyringError se o chaveiro falhar."""
    value = value.strip()
    if value:
        keyring.set_password(APP_NAME, name, value)
        return
    try:
        keyring.delete_password(APP_NAME, name)
    except PasswordDeleteError:
        pass
