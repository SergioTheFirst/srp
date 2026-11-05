"""Ed25519 (client/ed25519.py): векторы RFC 8032 §7.1 и отказ на любой порче."""

from __future__ import annotations

import pytest
from client import ed25519

pytestmark = pytest.mark.unit

# RFC 8032 §7.1, TEST 1-3: (seed, public, message, signature) -- сверено с текстом RFC.
VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e"
        "39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f"
        "3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67"
        "f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


@pytest.mark.parametrize(("seed", "public", "msg", "sig"), VECTORS)
def test_rfc8032_vectors(seed: str, public: str, msg: str, sig: str) -> None:
    seed_b, msg_b = bytes.fromhex(seed), bytes.fromhex(msg)
    assert ed25519.public_key(seed_b).hex() == public
    assert ed25519.sign(seed_b, msg_b).hex() == sig
    assert ed25519.verify(bytes.fromhex(public), msg_b, bytes.fromhex(sig))


def test_base_point_encodes_to_the_standard_value() -> None:
    assert ed25519._encode(ed25519._B).hex() == "58" + "66" * 31


def _vec3() -> tuple[bytes, bytes, bytes]:
    _, public, msg, sig = (bytes.fromhex(v) for v in VECTORS[2])
    return public, msg, sig


def _flip(data: bytes, index: int) -> bytes:
    return data[:index] + bytes([data[index] ^ 0x01]) + data[index + 1 :]


@pytest.mark.parametrize("where", ["msg", "r", "s", "key"])
def test_any_tampering_is_rejected(where: str) -> None:
    public, msg, sig = _vec3()
    if where == "msg":
        msg = _flip(msg, 0)
    elif where == "r":
        sig = _flip(sig, 0)
    elif where == "s":
        sig = _flip(sig, 40)
    else:
        public = _flip(public, 0)
    assert not ed25519.verify(public, msg, sig)


def test_non_canonical_s_is_rejected() -> None:
    # S + L проверяет то же уравнение; без S < L подпись можно размножить (RFC 8032 §8.4).
    public, msg, sig = _vec3()
    s = int.from_bytes(sig[32:], "little") + ed25519._L
    assert not ed25519.verify(public, msg, sig[:32] + s.to_bytes(32, "little"))


@pytest.mark.parametrize(("pub_len", "sig_len"), [(31, 64), (33, 64), (32, 63), (32, 65)])
def test_wrong_lengths_are_rejected_without_raising(pub_len: int, sig_len: int) -> None:
    public, msg, sig = _vec3()
    assert not ed25519.verify((public * 2)[:pub_len], msg, (sig * 2)[:sig_len])


@pytest.mark.parametrize("y", [ed25519._P, ed25519._P + 1, 2**255 - 1])
def test_non_canonical_y_does_not_decode(y: int) -> None:
    assert ed25519._decode(y.to_bytes(32, "little")) is None


def test_x_zero_with_sign_bit_does_not_decode() -> None:
    # (0, -1): y = p - 1, x = 0. Кодировка с x_0 = 1 запрещена (RFC 8032 §5.1.3), эталон §6
    # её пропускал (x² не по модулю p).
    assert ed25519._decode((ed25519._P - 1).to_bytes(32, "little")) is not None
    assert ed25519._decode(((ed25519._P - 1) | (1 << 255)).to_bytes(32, "little")) is None


@pytest.mark.parametrize("size", [0, 31, 33])
def test_seed_must_be_32_bytes(size: int) -> None:
    with pytest.raises(ValueError):
        ed25519.sign(b"\x01" * size, b"")
    with pytest.raises(ValueError):
        ed25519.public_key(b"\x01" * size)


def test_matches_pyca_cryptography() -> None:
    ref = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    for i in range(6):
        seed = bytes((i * 37 + j) % 256 for j in range(32))
        msg = bytes(range(i * 13))
        key = ref.Ed25519PrivateKey.from_private_bytes(seed)
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        assert ed25519.public_key(seed) == public
        assert ed25519.sign(seed, msg) == key.sign(msg)
        assert ed25519.verify(public, msg, key.sign(msg))
