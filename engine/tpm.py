"""TPM-bound password wrapping via the Microsoft Platform Crypto Provider (CNG / NCrypt).

The stored Windows logon password is encrypted with RSA-OAEP using a non-exportable
RSA key that lives inside the machine's TPM 2.0 chip. The private key never leaves the
TPM, so a copy of ``profiles.enc`` taken off the disk (backup, stolen drive, another OS)
cannot be decrypted even if the outer DPAPI layer were broken. On the running machine,
LocalSystem (the WinIRFaceID service) asks the TPM to unwrap the password on demand.

Everything degrades gracefully: when no TPM is present, the platform provider is
unavailable, or the plaintext is larger than one RSA-OAEP block, callers fall back to the
existing DPAPI-only protection.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import os
import threading

logger = logging.getLogger(__name__)

MS_PLATFORM_CRYPTO_PROVIDER = "Microsoft Platform Crypto Provider"
DEFAULT_KEY_NAME = "WinIRFaceID-PasswordKey"
TPM_BLOB_PREFIX = "tpm1:"

NCRYPT_MACHINE_KEY_FLAG = 0x00000020
NCRYPT_SILENT_FLAG = 0x00000040
NCRYPT_PAD_OAEP_FLAG = 0x00000004
NTE_BAD_KEYSET = 0x80090016
NTE_NO_KEY = 0x8009000D
RSA_KEY_BITS = 2048
# RSA-OAEP with SHA-256: k - 2*hLen - 2 = 256 - 64 - 2.
MAX_PLAINTEXT_BYTES = (RSA_KEY_BITS // 8) - 2 * 32 - 2

_DISABLE_ENV = "WINIRFACEID_DISABLE_TPM"


class TpmUnavailableError(RuntimeError):
    """Raised when the TPM key cannot be opened, created, or used."""


class _BCRYPT_OAEP_PADDING_INFO(ctypes.Structure):
    _fields_ = [
        ("pszAlgId", wintypes.LPCWSTR),
        ("pbLabel", ctypes.c_void_p),
        ("cbLabel", wintypes.ULONG),
    ]


_NCRYPT_HANDLE = ctypes.c_size_t
_ncrypt = None
_ncrypt_lock = threading.Lock()


def _lib():
    global _ncrypt
    with _ncrypt_lock:
        if _ncrypt is None:
            lib = ctypes.WinDLL("ncrypt")
            status = ctypes.c_long
            lib.NCryptOpenStorageProvider.argtypes = [
                ctypes.POINTER(_NCRYPT_HANDLE), wintypes.LPCWSTR, wintypes.DWORD,
            ]
            lib.NCryptOpenStorageProvider.restype = status
            lib.NCryptOpenKey.argtypes = [
                _NCRYPT_HANDLE, ctypes.POINTER(_NCRYPT_HANDLE), wintypes.LPCWSTR,
                wintypes.DWORD, wintypes.DWORD,
            ]
            lib.NCryptOpenKey.restype = status
            lib.NCryptCreatePersistedKey.argtypes = [
                _NCRYPT_HANDLE, ctypes.POINTER(_NCRYPT_HANDLE), wintypes.LPCWSTR,
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ]
            lib.NCryptCreatePersistedKey.restype = status
            lib.NCryptSetProperty.argtypes = [
                _NCRYPT_HANDLE, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
            ]
            lib.NCryptSetProperty.restype = status
            lib.NCryptFinalizeKey.argtypes = [_NCRYPT_HANDLE, wintypes.DWORD]
            lib.NCryptFinalizeKey.restype = status
            for fn in (lib.NCryptEncrypt, lib.NCryptDecrypt):
                fn.argtypes = [
                    _NCRYPT_HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                ]
                fn.restype = status
            lib.NCryptDeleteKey.argtypes = [_NCRYPT_HANDLE, wintypes.DWORD]
            lib.NCryptDeleteKey.restype = status
            lib.NCryptFreeObject.argtypes = [_NCRYPT_HANDLE]
            lib.NCryptFreeObject.restype = status
            _ncrypt = lib
        return _ncrypt


def _check(rc: int, what: str) -> None:
    if rc != 0:
        raise TpmUnavailableError(f"{what} failed (0x{rc & 0xFFFFFFFF:08X})")


def tpm_disabled_by_env() -> bool:
    """Return True when ``WINIRFACEID_DISABLE_TPM=1`` opts out of TPM wrapping (tests/dev)."""
    return os.environ.get(_DISABLE_ENV, "").strip() in ("1", "true", "yes")


class TpmKeyProtector:
    """Wrap/unwrap short secrets with a persisted, non-exportable TPM RSA key."""

    def __init__(self, key_name: str = DEFAULT_KEY_NAME, *, machine_key: bool = True) -> None:
        self.key_name = key_name
        self.machine_key = machine_key
        self._lock = threading.Lock()

    @property
    def _key_flags(self) -> int:
        return NCRYPT_SILENT_FLAG | (NCRYPT_MACHINE_KEY_FLAG if self.machine_key else 0)

    def _open_provider(self) -> int:
        lib = _lib()
        h_prov = _NCRYPT_HANDLE()
        _check(
            lib.NCryptOpenStorageProvider(ctypes.byref(h_prov), MS_PLATFORM_CRYPTO_PROVIDER, 0),
            "NCryptOpenStorageProvider",
        )
        return h_prov.value

    def _open_key(self, h_prov: int, *, create: bool) -> int:
        lib = _lib()
        h_key = _NCRYPT_HANDLE()
        rc = lib.NCryptOpenKey(h_prov, ctypes.byref(h_key), self.key_name, 0, self._key_flags)
        if rc == 0:
            return h_key.value
        if not create or (rc & 0xFFFFFFFF) not in (NTE_BAD_KEYSET, NTE_NO_KEY):
            _check(rc, "NCryptOpenKey")

        h_key = _NCRYPT_HANDLE()
        _check(
            lib.NCryptCreatePersistedKey(
                h_prov, ctypes.byref(h_key), "RSA", self.key_name, 0, self._key_flags
            ),
            "NCryptCreatePersistedKey",
        )
        try:
            bits = wintypes.DWORD(RSA_KEY_BITS)
            _check(
                lib.NCryptSetProperty(h_key.value, "Length", ctypes.byref(bits), 4, 0),
                "NCryptSetProperty(Length)",
            )
            _check(lib.NCryptFinalizeKey(h_key.value, NCRYPT_SILENT_FLAG), "NCryptFinalizeKey")
        except Exception:
            lib.NCryptFreeObject(h_key.value)
            raise
        logger.info("Created TPM-bound RSA-%d key '%s'", RSA_KEY_BITS, self.key_name)
        return h_key.value

    def _crypt(self, data: bytes, *, encrypt: bool) -> bytes:
        lib = _lib()
        with self._lock:
            h_prov = self._open_provider()
            try:
                h_key = self._open_key(h_prov, create=encrypt)
                try:
                    padding = _BCRYPT_OAEP_PADDING_INFO("SHA256", None, 0)
                    fn = lib.NCryptEncrypt if encrypt else lib.NCryptDecrypt
                    in_buf = ctypes.create_string_buffer(data, len(data))
                    out_len = wintypes.DWORD(0)
                    _check(
                        fn(h_key, in_buf, len(data), ctypes.byref(padding), None, 0,
                           ctypes.byref(out_len), NCRYPT_PAD_OAEP_FLAG | NCRYPT_SILENT_FLAG),
                        "NCryptEncrypt(size)" if encrypt else "NCryptDecrypt(size)",
                    )
                    out_buf = ctypes.create_string_buffer(out_len.value)
                    _check(
                        fn(h_key, in_buf, len(data), ctypes.byref(padding), out_buf, out_len.value,
                           ctypes.byref(out_len), NCRYPT_PAD_OAEP_FLAG | NCRYPT_SILENT_FLAG),
                        "NCryptEncrypt" if encrypt else "NCryptDecrypt",
                    )
                    result = out_buf.raw[: out_len.value]
                    ctypes.memset(out_buf, 0, len(out_buf))
                    ctypes.memset(in_buf, 0, len(in_buf))
                    return result
                finally:
                    lib.NCryptFreeObject(h_key)
            finally:
                lib.NCryptFreeObject(h_prov)

    def wrap(self, plaintext: bytes) -> bytes:
        """Encrypt ``plaintext`` (<= ``MAX_PLAINTEXT_BYTES``) with the TPM key, creating it if needed."""
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise TpmUnavailableError("Secret too long for a single RSA-OAEP block")
        return self._crypt(plaintext, encrypt=True)

    def unwrap(self, ciphertext: bytes) -> bytes:
        """Decrypt a blob produced by :meth:`wrap` inside the TPM."""
        return self._crypt(ciphertext, encrypt=False)

    def delete_key(self) -> None:
        """Delete the persisted TPM key (irreversibly invalidates every wrapped blob)."""
        lib = _lib()
        with self._lock:
            h_prov = self._open_provider()
            try:
                h_key = self._open_key(h_prov, create=False)
                _check(lib.NCryptDeleteKey(h_key, 0), "NCryptDeleteKey")
            finally:
                lib.NCryptFreeObject(h_prov)
