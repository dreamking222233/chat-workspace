from app.core.security import hash_password, verify_password


def test_password_hash_round_trip():
    encoded = hash_password("password123")
    assert encoded != "password123"
    assert verify_password("password123", encoded)
    assert not verify_password("wrong-password", encoded)
