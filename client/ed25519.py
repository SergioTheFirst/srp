"""Ed25519 (RFC 8032) на чистом stdlib: подпись пакета обновления агента.

Сборка подписывает пакет закрытым ключом, который не покидает машину сборки; агент проверяет
подпись одним из открытых ключей, вшитых в него (``client/update_trust.py``). Перенос эталона
RFC 8032 §6 с тремя правками:

- длины ключа и подписи проверяются (в эталоне пропущен ``raise``, erratum 5930);
- x² берётся по модулю p, иначе неканоничная кодировка точки (0, -1) с x_0 = 1 проходит
  декодирование вопреки §5.1.3;
- координаты сокращаются по модулю p после каждого сложения.

Проверка -- §5.1.7 в бескофакторной форме [S]B = R + [k]A (RFC: «sufficient»), S < L
обязателен (без него подпись можно размножить, §8.4). Время не константное: ``verify`` работает
с открытыми данными, а ``sign`` запускается раз на релиз на машине сборки.
"""

from __future__ import annotations

import hashlib
from typing import Optional, Tuple

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493  # порядок базовой точки
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

# Точка в расширенных координатах (X, Y, Z, T): x = X/Z, y = Y/Z, x*y = T/Z.
_Point = Tuple[int, int, int, int]
_IDENTITY: _Point = (0, 1, 1, 0)


def _add(a: _Point, b: _Point) -> _Point:
    pa = (a[1] - a[0]) * (b[1] - b[0]) % _P
    pb = (a[1] + a[0]) * (b[1] + b[0]) % _P
    pc = 2 * a[3] * b[3] * _D % _P
    pd = 2 * a[2] * b[2] % _P
    e, f, g, h = pb - pa, pd - pc, pd + pc, pb + pa
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(scalar: int, point: _Point) -> _Point:
    acc = _IDENTITY
    while scalar > 0:
        if scalar & 1:
            acc = _add(acc, point)
        point = _add(point, point)
        scalar >>= 1
    return acc


def _equal(a: _Point, b: _Point) -> bool:
    # x1/z1 == x2/z2 и y1/z1 == y2/z2 без деления
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y: int, sign: int) -> Optional[int]:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    return _P - x if (x & 1) != sign else x


def _decode(data: bytes) -> Optional[_Point]:
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _encode(point: _Point) -> bytes:
    z_inv = pow(point[2], _P - 2, _P)
    x = point[0] * z_inv % _P
    y = point[1] * z_inv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _base_point() -> _Point:
    y = 4 * pow(5, _P - 2, _P) % _P
    x = _recover_x(y, 0)
    if x is None:  # pragma: no cover -- константы кривой; тест сверяет кодировку B
        raise ArithmeticError("ed25519: базовая точка не восстановилась")
    return (x, y, 1, x * y % _P)


_B = _base_point()


def _hash_int(*parts: bytes) -> int:
    return int.from_bytes(hashlib.sha512(b"".join(parts)).digest(), "little") % _L


def _expand(seed: bytes) -> Tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("ed25519: закрытый ключ (seed) должен быть 32 байта")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def public_key(seed: bytes) -> bytes:
    """32 байта открытого ключа для 32-байтового seed."""
    return _encode(_mul(_expand(seed)[0], _B))


def sign(seed: bytes, msg: bytes) -> bytes:
    """64 байта подписи R || S, детерминированной (RFC 8032 §5.1.6)."""
    scalar, prefix = _expand(seed)
    public = _encode(_mul(scalar, _B))
    r = _hash_int(prefix, msg)
    r_enc = _encode(_mul(r, _B))
    s = (r + _hash_int(r_enc, public, msg) * scalar) % _L
    return r_enc + s.to_bytes(32, "little")


def verify(public: bytes, msg: bytes, signature: bytes) -> bool:
    """True только для подписи *msg* этим ключом; любой мусор на входе -> False."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a_point = _decode(public)
    r_point = _decode(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a_point is None or r_point is None or s >= _L:
        return False
    k = _hash_int(signature[:32], public, msg)
    return _equal(_mul(s, _B), _add(r_point, _mul(k, a_point)))
